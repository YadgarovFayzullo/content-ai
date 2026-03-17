import os
import json
from google import genai
from google.genai import types
from dotenv import load_dotenv
from database import Fact, is_fact_duplicate, save_fact
from pathlib import Path
import time

load_dotenv()

client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

# Список запрещенных тем/слов
BANNED_WORDS = [
    "o'lim", "olim", "terror", "din", "ekstremizm", "shariat", "aqida", "mazhab", "islom", "xristian",
    "смерть", "террор", "религия", "экстремизм", "шариат", "ислам", "христианство",
    "urush", "qon", "fashizm", "siyosat", "prezident", "hukumat", "davlat", "deputat", "hokim",
    "война", "кровь", "фашизм", "политика", "президент", "правительство", "депутат", "хоким",
    "shafqatsiz", "qiynoq", "o'ldir", "jinoyat", "qamoq", "o'g'ri", "firibgar",
    "жестокость", "пытка", "убийство", "преступление", "тюрьма", "вор",
    "kasallik", "rak", "falokat", "fajia", "baxtsiz", "tushkunlik", "depressiya", "vafot",
    "болезнь", "рак", "катастрофа", "трагедия", "несчастье", "депрессия", "умер",
    "narkotik", "spirt", "alkogol", "sigaret", "nasha", "giyohvand"
]

STYLE_PROMPT = (
    "Editorial gouache illustration of silhouettes interacting with floating books and symbols of knowledge "
    "in a surreal abstract educational space. Rough brush strokes, thick paint texture, matte gouache colors, "
    "dynamic composition, conceptual art about learning and thinking, minimalist background, 2D flat editorial illustration. "
    "Focus on the visual metaphor of: {subject}"
)

def is_safe(text: str) -> bool:
    """Программная проверка (последний рубеж)."""
    text_lower = text.lower()
    for word in BANNED_WORDS:
        if word in text_lower:
            return False
    return True

def generate_fact_text():
    """Генерирует текст, внедряя бан-лист прямо в промпт."""
    # Превращаем список слов в строку для промпта
    ban_list_str = ", ".join(BANNED_WORDS)
    
    prompt = (
        "Psixologiya, fan yoki texnologiya haqida bitta qiziqarli mini-fakt tayyorlang. "
        "MUHIM QOIDA: Quyidagi so'zlardan va mavzulardan foydalanish QAT'IYAN TAQIQLANADI: "
        f"[{ban_list_str}]. "
        "Fakt faqat pozitiv yoki neytral-ilm-fan yo'nalishida bo'lsin. "
        "Javobni FAQAT JSON formatida bering: {'fact': '...', 'explanation': '...', 'hashtags': ['#...', '#...', '#...']} "
        "Barcha matnlar O'ZBEK tilida bo'lishi shart."
    )
    
    for attempt in range(3): # Уменьшаем кол-во итераций, так как промпт теперь точнее
        try:
            response = client.models.generate_content(
                model='gemini-2.0-flash',
                config=types.GenerateContentConfig(response_mime_type='application/json'),
                contents=prompt
            )
            data = json.loads(response.text)
            
            full_text = f"{data['fact']} {data['explanation']}"
            if is_safe(full_text) and not is_fact_duplicate(data['fact']):
                return data
            else:
                print(f"Filter triggerlandi (urinish {attempt+1}). Qayta yuborilmoqda...")
                
        except Exception as e:
            print(f"Xatolik (TEXT): {e}")
            continue
    return None

def generate_illustration(subject_meta, fact_id_str):
    """Генерация через nano-banana."""
    full_prompt = STYLE_PROMPT.format(subject=subject_meta)
    model_name = 'nano-banana-pro-preview'
    
    try:
        print(f"Gugl Nano Banana orqali rasm yaratilmoqda...")
        response = client.models.generate_content(
            model=model_name,
            contents=full_prompt
        )
        image_data = response.candidates[0].content.parts[0].inline_data.data
        
        output_dir = Path("gen_images")
        output_dir.mkdir(exist_ok=True)
        file_path = output_dir / f"fact_{fact_id_str}.png"
        
        with open(file_path, "wb") as f:
            f.write(image_data)
        return str(file_path)
    except Exception as e:
        print(f"Rasm yaratishda xato: {e}")
        return None

def create_daily_content():
    print("--- CONTENT YARATISH BOSHLANDI ---")
    fact_data = generate_fact_text()
    if not fact_data: return None
    
    fact_id_str = str(int(time.time()))
    image_path = generate_illustration(fact_data['explanation'] or fact_data['fact'], fact_id_str)
    if not image_path: return None

    fact_entry = Fact(
        text=fact_data['fact'],
        image_prompt=fact_data['explanation'],
        image_url=image_path,
        posted=False
    )
    
    return {"data": fact_data, "image_url": image_path, "entry": fact_entry}
