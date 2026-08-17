# Punto de entrada para Vercel (función Python serverless).
# El backend real vive en masscob-b2b/backend/ (mismo código que se corre en
# local con `uvicorn main:app --reload`); esto solo lo hace localizable
# porque Vercel expone automáticamente cualquier .py bajo /api.
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "masscob-b2b", "backend"))

from main import app  # noqa: E402
