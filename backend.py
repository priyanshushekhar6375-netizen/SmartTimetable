"""
TimeOptima - Backend entry point and compatibility bridge.
Exposes the FastAPI application from main.py for uvicorn (backend:app).
"""

import uvicorn
from main import app, init_db

# Ensure database is initialized
init_db()

if __name__ == "__main__":
    uvicorn.run("backend:app", host="127.0.0.1", port=8000, reload=True)

