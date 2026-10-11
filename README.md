# TimeOptima - Smart Timetable & Resource Optimizer

TimeOptima is an automated college and institutional timetable scheduling platform powered by constraint satisfaction programming (Google OR-Tools) with a FastAPI backend and a responsive modern web dashboard.

---

## 🚀 Features

- **Constraint-Based Optimization Engine**: Employs Google OR-Tools CP-SAT solver to automatically generate clash-free timetables satisfying hard and soft operational constraints (room capacities, teacher availability, lab equipment, etc.).
- **Institutional Management**: Manage sections, rooms, laboratories, teachers, subjects, and timeslots.
- **Interactive Web Interface**: Clean, responsive frontend dashboard (`index.html`, `style.css`, `script.js`) with schedule views, export options, and drag-and-drop / manual adjustments.
- **RESTful API**: FastAPI backend providing endpoints for database CRUD, solver invocations, analytics, and authentication.
- **Local Persistence**: Built-in SQLite database (`timetable.db`) with automatic table migration and initial seeding.

---

## 🛠️ Tech Stack

- **Backend**: Python 3.10+, [FastAPI](https://fastapi.tiangolo.com/), [Google OR-Tools](https://developers.google.com/optimization), [Uvicorn](https://www.uvicorn.org/), SQLite3
- **Frontend**: HTML5, CSS3, JavaScript (ES6+), FontAwesome

---

## 📦 Getting Started

### 1. Prerequisites
- Python 3.10 or higher
- Git

### 2. Installation

Clone the repository:
```bash
git clone https://github.com/priyanshushekhar6375-netizen/SmartTimetable.git
cd SmartTimetable
```

Install backend dependencies:
```bash
pip install -r requirements.txt
```

### 3. Running the Application

Start the FastAPI backend server:
```bash
python backend.py
```
Or with uvicorn directly:
```bash
uvicorn backend:app --host 127.0.0.1 --port 8000 --reload
```

The backend server will run on `http://127.0.0.1:8000`.

Open `index.html` in your browser (or serve it via Live Server/HTTP server) to access the TimeOptima interface.

---

## 🔐 Default Admin Credentials

- **Email**: `admin@college.edu`
- **Password**: `password123`
