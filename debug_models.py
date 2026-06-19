import os
from google import genai
from dotenv import load_dotenv

load_dotenv()

key = os.getenv("GEMINI_API_KEY")
if not key:
    print("Ошибка: GEMINI_API_KEY не найден в .env")
else:
    print(f"Ключ найден (начинается на {key[:4]}...)")
    client = genai.Client(api_key=key)

    print("\nСписок доступных моделей для вашего ключа:")
    try:
        for m in client.models.list():
            actions = getattr(m, "supported_actions", None) or []
            if "generateContent" in actions:
                print(f"- {m.name}")
    except Exception as e:
        print(f"Ошибка при получении списка моделей: {e}")
