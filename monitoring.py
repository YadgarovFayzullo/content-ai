"""Лёгкий мониторинг сервера (backend).

Собирает «здоровье» инфраструктуры одного хоста БЕЗ внешних агентов и тяжёлых
зависимостей: только Docker Engine API (через unix-сокет /var/run/docker.sock,
смонтирован в бот read-only) и /proc хоста.

Сигналы:
  • контейнеры          — up/down, перезапуски, health, CPU/RAM каждого;
  • ресурсы хоста        — CPU %, RAM %, диск %, load average;
  • ошибки в логах       — свежие ERROR/Exception/Traceback по контейнерам;
  • производные инциденты — то, что watcher шлёт супер-админу в Telegram.

Всё async (httpx поверх unix-транспорта). Вызывается из internal_api (страница
в админке через прокси) и из system_watch (пинги/дайджест в боте).
"""
import asyncio
import logging
import os
import re
import shutil
import time
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)

DOCKER_SOCK = os.getenv("DOCKER_SOCK", "/var/run/docker.sock")

# Какие контейнеры мы считаем «нашими сервисами» (для service-health и того,
# чьи логи сканировать на ошибки). Сопоставляем по подстроке в имени.
KNOWN_SERVICES = ("bot", "admin_api", "admin-api", "db", "nginx", "rag", "certbot")

# Сколько строк лога на контейнер тянуть и какие паттерны считать ошибкой.
LOG_TAIL = 400
_ERR_RE = re.compile(
    r"\b(ERROR|CRITICAL|FATAL|Traceback|Exception|panic)\b", re.IGNORECASE
)
# Шум, который НЕ считаем инцидентом (штатные сообщения уровня INFO со словом error и т.п.).
_ERR_IGNORE = re.compile(r"GET /health|favicon", re.IGNORECASE)

# Пороги для производных инцидентов (host).
CPU_WARN = 90.0
MEM_WARN = 90.0
DISK_WARN = 90.0


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _client() -> httpx.AsyncClient:
    """httpx-клиент поверх unix-сокета Docker. base_url хост игнорируется."""
    transport = httpx.AsyncHTTPTransport(uds=DOCKER_SOCK)
    return httpx.AsyncClient(transport=transport, base_url="http://docker", timeout=15.0)


# ---------------------------------------------------------------------------
# Docker: контейнеры + per-container CPU/RAM
# ---------------------------------------------------------------------------
def _calc_cpu_pct(stats: dict) -> float:
    """CPU % контейнера по дельте из одиночного snapshot (stream=false даёт и
    precpu_stats, поэтому дельта валидна без второго запроса)."""
    try:
        cpu = stats["cpu_stats"]
        pre = stats["precpu_stats"]
        cpu_delta = cpu["cpu_usage"]["total_usage"] - pre["cpu_usage"]["total_usage"]
        sys_delta = cpu.get("system_cpu_usage", 0) - pre.get("system_cpu_usage", 0)
        ncpu = cpu.get("online_cpus") or len(
            cpu["cpu_usage"].get("percpu_usage") or []
        ) or 1
        if sys_delta > 0 and cpu_delta > 0:
            return round((cpu_delta / sys_delta) * ncpu * 100.0, 1)
    except (KeyError, TypeError, ZeroDivisionError):
        pass
    return 0.0


def _calc_mem(stats: dict) -> tuple[float, float]:
    """(used_mb, pct) контейнера. Вычитаем page cache, как делает `docker stats`."""
    try:
        mem = stats["memory_stats"]
        usage = mem.get("usage", 0)
        cache = (mem.get("stats") or {}).get("inactive_file") or (
            mem.get("stats") or {}
        ).get("cache", 0)
        used = max(0, usage - cache)
        limit = mem.get("limit", 0) or 1
        return round(used / 1048576, 1), round(used / limit * 100, 1)
    except (KeyError, TypeError, ZeroDivisionError):
        return 0.0, 0.0


async def _container_stats(client: httpx.AsyncClient, cid: str) -> dict:
    try:
        r = await client.get(f"/containers/{cid}/stats", params={"stream": "false"})
        r.raise_for_status()
        return r.json()
    except Exception as e:  # noqa: BLE001
        logger.debug("stats failed for %s: %s", cid, e)
        return {}


def _demux_logs(raw: bytes) -> str:
    """Docker отдаёт логи non-TTY контейнера мультиплексом: каждый кадр —
    8-байтовый заголовок (stream-тип + 4 байта длины big-endian) + payload.
    Склеиваем payload'ы обратно в текст."""
    out, i, n = [], 0, len(raw)
    while i + 8 <= n:
        size = int.from_bytes(raw[i + 4 : i + 8], "big")
        i += 8
        out.append(raw[i : i + size])
        i += size
    if not out:  # TTY-контейнер или другой формат — отдаём как есть
        return raw.decode("utf-8", "replace")
    return b"".join(out).decode("utf-8", "replace")


async def _container_log_errors(client: httpx.AsyncClient, cid: str, name: str) -> dict:
    """Свежие ошибки в логе контейнера: {container, count, last, sample}."""
    try:
        r = await client.get(
            f"/containers/{cid}/logs",
            params={
                "stdout": "true",
                "stderr": "true",
                "tail": str(LOG_TAIL),
                "timestamps": "true",
            },
        )
        r.raise_for_status()
        text = _demux_logs(r.content)
    except Exception as e:  # noqa: BLE001
        logger.debug("logs failed for %s: %s", cid, e)
        return {"container": name, "count": 0, "last": None, "sample": None}

    hits = [
        ln.strip()
        for ln in text.splitlines()
        if _ERR_RE.search(ln) and not _ERR_IGNORE.search(ln)
    ]
    return {
        "container": name,
        "count": len(hits),
        "last": hits[-1][:300] if hits else None,
        "sample": [h[:300] for h in hits[-5:]] if hits else [],
    }


# ---------------------------------------------------------------------------
# Хост: CPU / RAM / диск / load
# ---------------------------------------------------------------------------
def _read_cpu_times() -> tuple[int, int]:
    """(idle, total) из первой строки /proc/stat хоста."""
    with open("/proc/stat", "r") as f:
        parts = f.readline().split()[1:]
    nums = [int(x) for x in parts]
    idle = nums[3] + (nums[4] if len(nums) > 4 else 0)  # idle + iowait
    return idle, sum(nums)


async def _host_cpu_pct() -> float:
    try:
        idle0, total0 = _read_cpu_times()
        await asyncio.sleep(0.4)
        idle1, total1 = _read_cpu_times()
        dt = total1 - total0
        di = idle1 - idle0
        if dt > 0:
            return round((1 - di / dt) * 100, 1)
    except Exception as e:  # noqa: BLE001
        logger.debug("host cpu read failed: %s", e)
    return 0.0


def _host_mem() -> dict:
    try:
        info = {}
        with open("/proc/meminfo", "r") as f:
            for line in f:
                k, _, v = line.partition(":")
                info[k.strip()] = int(v.split()[0])  # kB
        total = info.get("MemTotal", 0)
        avail = info.get("MemAvailable", info.get("MemFree", 0))
        used = total - avail
        return {
            "total_mb": round(total / 1024),
            "used_mb": round(used / 1024),
            "pct": round(used / total * 100, 1) if total else 0.0,
        }
    except Exception as e:  # noqa: BLE001
        logger.debug("host mem read failed: %s", e)
        return {"total_mb": 0, "used_mb": 0, "pct": 0.0}


def _host_disk() -> dict:
    try:
        t, u, _ = shutil.disk_usage("/")
        return {
            "total_gb": round(t / 1073741824, 1),
            "used_gb": round(u / 1073741824, 1),
            "pct": round(u / t * 100, 1) if t else 0.0,
        }
    except Exception as e:  # noqa: BLE001
        logger.debug("host disk read failed: %s", e)
        return {"total_gb": 0, "used_gb": 0, "pct": 0.0}


def _host_misc() -> dict:
    out = {"load1": 0.0, "load5": 0.0, "load15": 0.0, "ncpu": os.cpu_count() or 1}
    try:
        with open("/proc/loadavg", "r") as f:
            a, b, c = f.read().split()[:3]
        out.update(load1=float(a), load5=float(b), load15=float(c))
    except Exception:  # noqa: BLE001
        pass
    return out


async def _host() -> dict:
    cpu = await _host_cpu_pct()
    return {"cpu_pct": cpu, "mem": _host_mem(), "disk": _host_disk(), **_host_misc()}


# ---------------------------------------------------------------------------
# Сборка полного статуса
# ---------------------------------------------------------------------------
async def collect_status(*, with_logs: bool = True) -> dict:
    """Полный снимок здоровья хоста. Не бросает — при недоступности Docker
    возвращает докер-секцию с ошибкой, но хост-метрики всё равно отдаёт."""
    host_task = asyncio.create_task(_host())
    containers: list[dict] = []
    log_errors: list[dict] = []
    docker_ok = True
    docker_error = None

    try:
        async with _client() as client:
            r = await client.get("/containers/json", params={"all": "true"})
            r.raise_for_status()
            raw = r.json()

            # Stats только по running-контейнерам, параллельно.
            running = [c for c in raw if c.get("State") == "running"]
            stats_list = await asyncio.gather(
                *[_container_stats(client, c["Id"]) for c in running]
            )
            stats_by_id = {c["Id"]: s for c, s in zip(running, stats_list)}

            for c in raw:
                cid = c["Id"]
                name = (c.get("Names") or ["/?"])[0].lstrip("/")
                st = stats_by_id.get(cid, {})
                used_mb, mem_pct = _calc_mem(st) if st else (0.0, 0.0)
                containers.append(
                    {
                        "name": name,
                        "image": c.get("Image", ""),
                        "state": c.get("State", "unknown"),
                        "status": c.get("Status", ""),
                        "health": _health_of(c),
                        "cpu_pct": _calc_cpu_pct(st) if st else 0.0,
                        "mem_mb": used_mb,
                        "mem_pct": mem_pct,
                    }
                )

            if with_logs:
                # Сканируем логи только наших сервисов (не сторонних контейнеров).
                ours = [
                    c
                    for c in raw
                    if any(s in (c.get("Names") or [""])[0] for s in KNOWN_SERVICES)
                ]
                log_errors = await asyncio.gather(
                    *[
                        _container_log_errors(
                            client, c["Id"], (c.get("Names") or ["/?"])[0].lstrip("/")
                        )
                        for c in ours
                    ]
                )
                log_errors = [e for e in log_errors if e["count"] > 0]
    except Exception as e:  # noqa: BLE001
        docker_ok = False
        docker_error = str(e)
        logger.warning("Docker monitoring unavailable: %s", e)

    host = await host_task
    containers.sort(key=lambda c: (c["state"] != "running", c["name"]))

    snapshot = {
        "generated_at": _utcnow_iso(),
        "docker_ok": docker_ok,
        "docker_error": docker_error,
        "host": host,
        "containers": containers,
        "log_errors": log_errors,
        "services": _service_health(containers),
    }
    snapshot["incidents"] = derive_incidents(snapshot)
    return snapshot


def _health_of(c: dict) -> str | None:
    """healthcheck-статус из `Status` ('Up 2h (healthy)') — Docker не кладёт его
    в /containers/json отдельным полем, поэтому парсим текст."""
    status = c.get("Status", "")
    m = re.search(r"\((healthy|unhealthy|health: starting)\)", status)
    return m.group(1) if m else None


def _service_health(containers: list[dict]) -> list[dict]:
    """По одному статусу на ожидаемый сервис: ok, если есть running-контейнер
    с подходящим именем и health != unhealthy."""
    out = []
    seen = set()
    for svc in ("bot", "admin_api", "db", "nginx", "rag", "certbot"):
        if svc in seen:
            continue
        seen.add(svc)
        match = next(
            (c for c in containers if svc.replace("_", "") in c["name"].replace("_", "")),
            None,
        )
        if match is None:
            out.append({"name": svc, "ok": None, "detail": "not found"})
        else:
            ok = match["state"] == "running" and match["health"] != "unhealthy"
            out.append(
                {"name": svc, "ok": ok, "detail": match["status"] or match["state"]}
            )
    return out


def derive_incidents(snapshot: dict) -> list[dict]:
    """Производные инциденты — то, что заслуживает пинга. severity: warn|crit."""
    inc: list[dict] = []
    if not snapshot["docker_ok"]:
        inc.append(
            {"severity": "crit", "kind": "docker", "message": "Docker API недоступен"}
        )

    for c in snapshot["containers"]:
        # Сервис лёг (но не одноразовые job-контейнеры в состоянии exited(0)).
        if c["state"] in ("exited", "dead") and any(
            s in c["name"] for s in ("bot", "admin", "db", "nginx", "rag")
        ):
            inc.append(
                {
                    "severity": "crit",
                    "kind": "container_down",
                    "message": f"Контейнер {c['name']} не работает ({c['status']})",
                }
            )
        if c["health"] == "unhealthy":
            inc.append(
                {
                    "severity": "crit",
                    "kind": "unhealthy",
                    "message": f"Контейнер {c['name']} unhealthy",
                }
            )

    host = snapshot["host"]
    if host["cpu_pct"] >= CPU_WARN:
        inc.append(
            {"severity": "warn", "kind": "cpu", "message": f"CPU {host['cpu_pct']}%"}
        )
    if host["mem"]["pct"] >= MEM_WARN:
        inc.append(
            {"severity": "warn", "kind": "mem", "message": f"RAM {host['mem']['pct']}%"}
        )
    if host["disk"]["pct"] >= DISK_WARN:
        inc.append(
            {
                "severity": "warn",
                "kind": "disk",
                "message": f"Диск {host['disk']['pct']}% ({host['disk']['used_gb']}/{host['disk']['total_gb']} GB)",
            }
        )

    for e in snapshot["log_errors"]:
        if e["count"] > 0:
            inc.append(
                {
                    "severity": "warn",
                    "kind": "log_errors",
                    "message": f"{e['container']}: {e['count']} ошибок в логе",
                    "detail": e["last"],
                }
            )
    return inc
