#uvicorn main:app --reload)
import os
import re
import json
from datetime import datetime, timedelta
from typing import List, Optional
from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr, validator
from passlib.context import CryptContext
from google import genai
from google.genai import types
from icalendar import Calendar, Event

# -----------------------------------------------------------------------------
# CONFIGURACIÓN GENERAL
# -----------------------------------------------------------------------------
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "TU_API_KEY_AQUI")

client = genai.Client(api_key=GEMINI_API_KEY)

app = FastAPI(
    title="Planner AI API",
    version="3.5.0",
    description="Backend oficial de producción para Planner AI SaaS"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Control en memoria de agendas activas por usuario
USER_PLANS_DB = {}

# -----------------------------------------------------------------------------
# MODELOS DE DATOS Y SCHEMAS
# -----------------------------------------------------------------------------
class UserRegister(BaseModel):
    email: EmailStr
    full_name: str
    password: str

    @validator('password')
    def validate_password_strength(cls, v):
        if len(v) < 8:
            raise ValueError('La contraseña debe tener al menos 8 caracteres.')
        if not re.search(r'[A-Z]', v):
            raise ValueError('Debe contener al menos una letra mayúscula.')
        if not re.search(r'[a-z]', v):
            raise ValueError('Debe contener al menos una letra minúscula.')
        if not re.search(r'\d', v):
            raise ValueError('Debe contener al menos un número.')
        if not re.search(r'[@$!%*?&#]', v):
            raise ValueError('Debe contener al menos un carácter especial (@$!%*?&#).')
        return v

class UserLogin(BaseModel):
    email: EmailStr
    password: str

class PromptInput(BaseModel):
    user_id: str
    raw_prompt: str
    available_hours: str
    user_plan: str = "FREE"

class ModifyScheduleInput(BaseModel):
    current_schedule: List[dict]
    modification_request: str

class PaymentProcessInput(BaseModel):
    user_email: str
    card_number: str
    card_holder: str
    expiry: str
    cvc: str

class ResourceLink(BaseModel):
    title: str
    url: str

class TimeBlockOutput(BaseModel):
    title: str
    description: str
    day: str
    start_time: str
    end_time: str
    category: str
    detailed_guide: str
    recommended_resources: List[ResourceLink]

# -----------------------------------------------------------------------------
# ENDPOINTS DE AUTENTICACIÓN Y SUSCRIPCIÓN CON TARJETA
# -----------------------------------------------------------------------------
@app.post("/api/v1/auth/register", status_code=status.HTTP_201_CREATED)
def register_user(user: UserRegister):
    return {
        "message": "Cuenta creada exitosamente.",
        "user": {"email": user.email, "full_name": user.full_name, "plan": "FREE"}
    }

@app.post("/api/v1/auth/login")
def login_user(user: UserLogin):
    return {
        "message": "Autenticación exitosa.",
        "user": {"email": user.email, "full_name": "Usuario Registrado", "plan": "FREE"}
    }

@app.post("/api/v1/payments/process-card")
def process_card_payment(payment: PaymentProcessInput):
    clean_card = payment.card_number.replace(" ", "")
    if len(clean_card) < 15 or len(payment.cvc) < 3:
        raise HTTPException(status_code=400, detail="Los datos ingresados de la tarjeta son inválidos.")
    
    return {
        "status": "SUCCESS",
        "message": "Suscripción PRO activada correctamente.",
        "transaction_id": f"TX_{datetime.now().strftime('%Y%m%d%H%M%S')}",
        "user_plan": "PRO"
    }

# -----------------------------------------------------------------------------
# GENERADOR Y MODIFICADOR DE AGENDA CON IA
# -----------------------------------------------------------------------------
@app.post("/api/v1/planner/generate", response_model=List[TimeBlockOutput])
def generate_schedule(data: PromptInput):
    current_active = USER_PLANS_DB.get(data.user_id, 0)
    max_allowed = 1 if data.user_plan == "FREE" else 3

    if current_active >= max_allowed:
        raise HTTPException(
            status_code=403,
            detail=f"Límite alcanzado: Tu plan {data.user_plan} solo permite {max_allowed} agenda(s) activa(s). Pásate al plan PRO ($6.99/mes) para gestionar hasta 3 simultáneas."
        )

    prompt_text = f"""
    Genera un itinerario semanal detallado para el siguiente objetivo: "{data.raw_prompt}".
    Disponibilidad: {data.available_hours}.

    Instrucciones:
    1. Divide la meta en sub-tareas ejecutables de entre 30 y 90 minutos.
    2. Clasifica cada tarea en: ESTUDIO, EJERCICIO, TRABAJO, DESCANSO, PERSONAL.
    3. Incluye una guía paso a paso y recursos recomendados en YouTube o web.

    Devuelve un JSON estricto:
    [
        {{
            "title": "Nombre de la sub-tarea",
            "description": "Explicación breve",
            "day": "Lunes",
            "start_time": "16:00",
            "end_time": "17:00",
            "category": "ESTUDIO",
            "detailed_guide": "Pasos a seguir...",
            "recommended_resources": [
                {{"title": "Tutorial o recurso sugerido", "url": "https://www.youtube.com/results?search_query=tutorial"}}
            ]
        }}
    ]
    """

    for model_name in ['gemini-3.8-flash', 'gemini-2.5-flash']:
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=prompt_text,
                config=types.GenerateContentConfig(response_mime_type="application/json")
            )
            schedule = json.loads(response.text)
            USER_PLANS_DB[data.user_id] = current_active + 1
            return schedule
        except Exception:
            continue

    raise HTTPException(status_code=503, detail="Servidores ocupados. Intente nuevamente en unos segundos.")

@app.post("/api/v1/planner/modify", response_model=List[TimeBlockOutput])
def modify_schedule(data: ModifyScheduleInput):
    prompt_text = f"""
    Tienes esta agenda actual en JSON: {json.dumps(data.current_schedule)}
    Aplica este cambio específico solicitado por el usuario: "{data.modification_request}".
    Responde ÚNICAMENTE con la lista JSON actualizada.
    """

    try:
        response = client.models.generate_content(
            model='gemini-3.8-flash',
            contents=prompt_text,
            config=types.GenerateContentConfig(response_mime_type="application/json")
        )
        return json.loads(response.text)
    except Exception as e:
        raise HTTPException(status_code=500, detail="No se pudo procesar el ajuste.")

@app.post("/api/v1/planner/clear")
def clear_user_plans(user_id: str):
    USER_PLANS_DB[user_id] = 0
    return {"message": "Datos eliminados correctamente."}

# -----------------------------------------------------------------------------
# EXPORTACIÓN A CALENDARIO (.ICS)
# -----------------------------------------------------------------------------
@app.post("/api/v1/planner/export-ics")
def export_to_ics(time_blocks: List[dict]):
    cal = Calendar()
    cal.add('prodid', '-//Planner AI App//ESPOL//EC')
    cal.add('version', '2.0')
    dias_semana = {"lunes": 0, "martes": 1, "miércoles": 2, "miercoles": 2, "jueves": 3, "viernes": 4, "sábado": 5, "sabado": 5, "domingo": 6}
    hoy = datetime.now()

    for block in time_blocks:
        event = Event()
        event.add('summary', block.get('title', 'Actividad Planner AI'))
        event.add('description', f"{block.get('description', '')}\n\nGuía: {block.get('detailed_guide', '')}")
        dia_nombre = str(block.get('day', '')).lower().strip()
        target_weekday = dias_semana.get(dia_nombre, hoy.weekday())
        days_ahead = target_weekday - hoy.weekday()
        if days_ahead < 0: days_ahead += 7
        target_date = hoy + timedelta(days=days_ahead)
        try:
            sh, sm = map(int, block.get('start_time', '09:00').split(':'))
            eh, em = map(int, block.get('end_time', '10:00').split(':'))
            event.add('dtstart', target_date.replace(hour=sh, minute=sm))
            event.add('dtend', target_date.replace(hour=eh, minute=em))
        except Exception:
            event.add('dtstart', target_date)
            event.add('dtend', target_date + timedelta(hours=1))
        cal.add_component(event)

    return {"ics_content": cal.to_ical().decode('utf-8')}