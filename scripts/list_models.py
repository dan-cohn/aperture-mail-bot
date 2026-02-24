#!/usr/bin/env python3
"""List available Gemini models for your API key."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import google.generativeai as genai
from config import settings

genai.configure(api_key=settings.gemini_api_key)
print("Models that support generateContent:\n")
for m in genai.list_models():
    if "generateContent" in m.supported_generation_methods:
        print(f"  {m.name}")
