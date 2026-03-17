import os
import google.generativeai as genai
from dotenv import load_dotenv

load_dotenv()

key = os.getenv("GEMINI_API_KEY")
if not key:
    print("Ошибка: GEMINI_API_KEY не найден в .env")
else:
    print(f"Ключ найден (начинается на {key[:4]}...)")
    genai.configure(api_key=key)

    print("\nСписок доступных моделей для вашего ключа:")
    try:
        models = genai.list_models()
        for m in models:
            if "generateContent" in m.supported_generation_methods:
                print(f"- {m.name}")
    except Exception as e:
        print(f"Ошибка при получении списка моделей: {e}")
