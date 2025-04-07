from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional, List, Dict
import requests
import os
from fastapi.middleware.cors import CORSMiddleware
import json
import sqlite3
import uuid

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

AZURE_API_KEY = "DZKAe2jMOWbZJlqrBurzm0p2wU4lAoJ7BvAb97jlXZWXu3q5iCEfJQQJ99BDACHYHv6XJ3w3AAABACOGBq1S"
AZURE_ENDPOINT = "https://rag-codec-v1.openai.azure.com/"
AZURE_DEPLOYMENT = "gpt-4o-mini-codec"
AZURE_API_VERSION = "2024-02-15-preview"

SALES_DEPARTMENT = "18296531398"

# Diccionario para almacenar consultas de precios pendientes
pending_price_queries = {}


def init_db():
    conn = sqlite3.connect('chatbot.db')
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS conversations
        (phone_number TEXT PRIMARY KEY,
         conversation_history TEXT,
         last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP)
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS pending_queries
        (query_id TEXT PRIMARY KEY,
         customer_phone TEXT,
         question TEXT,
         timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP)
    ''')
    conn.commit()
    conn.close()


init_db()

STORE_INFO = """
Soy el asistente virtual de Franyer Mobile Center, una tienda especializada en venta y reparación de teléfonos.

Ubicaciones:
- San Francisco de Macorís: Calle Salcedo #74 entre Colón y 27 de Febrero
  Teléfonos: 849-342-1998 / 829-551-0000
  Ubicación: https://www.google.com/maps/place/Franyer+Mobile+Center/@19.2944188,-70.25871,17z/
- Castillo: Teléfono: 809-313-2513
  Ubicación: https://maps.app.goo.gl/gaCooEW63uf7FQTA6

Instagram: https://www.instagram.com/franyer.mcsfm/

Cuentas bancarias:
- Asociación Duarte: FRANYER MOBILE CENTER SRL - 0070119052
- Banreservas: Franyer B almonte (Ahorro) - 9100035114
- BHD: Franyer B almonte (Ahorro) - 29849000010

Tipos de Pantallas que utilizamos:
Incell: Pantalla delgada, buena respuesta táctil, colores decentes. Más económica.
OLED: Colores vivos, negros reales, mejor contraste. Más nítida.
Soft OLED: Más flexible y resistente. Mejor calidad que OLED normal.
Original: La de fábrica. Máxima calidad, brillo, sensibilidad y duración.

Averias tradicionales por las que el usuario consultara:
- Telefono mojado: Indicale que lo apague y no lo entre en arroz, debe apagarlo y dirijirse a una de nuestras sucursales mas cercanas.
- No tengo señal: Reiniciar el telefono, verificar la bandeja del telefono, de continuar igual dirijirse a una de nuestras sucursales mas cercanas.
"""

SYSTEM_PROMPT = f"""
Eres un asistente amable y profesional de Franyer Mobile Center. 
{STORE_INFO}

Directrices importantes:
1. Sé amable y profesional en todo momento
2. Habla de forma natural, como un humano
3. Siempre invita a los clientes a visitar nuestras sucursales para un mejor servicio
4. Proporciona información precisa sobre ubicaciones y contactos
5. Si un cliente tiene un problema técnico, sugiérele visitar una sucursal para diagnóstico presencial
6. Usa emojis ocasionalmente para dar un tono amigable
"""

PRICE_DETECTION_PROMPT = """
Eres un analizador de mensajes para una tienda de teléfonos móviles. Tu tarea es identificar si un mensaje contiene una consulta sobre precios de productos o servicios.

Instrucciones:
1. Identifica si el mensaje contiene una consulta sobre precios de cualquier producto o servicio.
2. Responde únicamente con "SI" si el mensaje contiene una consulta de precio, o "NO" si no la contiene.

Ejemplos de consultas de precio:
- "Cuánto cuesta un iPhone 13?"
- "Precio de cambio de pantalla para Samsung S21"
- "Tienen protectores? Cuánto valen?"
- "Me interesa un cargador, a cuánto lo tienen?"

Ejemplos que NO son consultas de precio:
- "Dónde está ubicada la tienda?"
- "Qué horario tienen?"
- "Mi teléfono no enciende"
- "Necesito reparar mi celular"
"""


class ChatRequest(BaseModel):
    question: str
    phone_number: str


class PriceResponse(BaseModel):
    query_id: str
    price_info: str


def get_conversation_history(phone_number: str) -> List[dict]:
    conn = sqlite3.connect('chatbot.db')
    c = conn.cursor()
    c.execute('SELECT conversation_history FROM conversations WHERE phone_number = ?', (phone_number,))
    result = c.fetchone()
    conn.close()

    if result:
        return json.loads(result[0])
    return []


def update_conversation_history(phone_number: str, conversation_history: List[dict]):
    conn = sqlite3.connect('chatbot.db')
    c = conn.cursor()

    history_json = json.dumps(conversation_history)

    c.execute('SELECT 1 FROM conversations WHERE phone_number = ?', (phone_number,))
    exists = c.fetchone() is not None

    if exists:
        c.execute('''
            UPDATE conversations 
            SET conversation_history = ?, last_updated = CURRENT_TIMESTAMP
            WHERE phone_number = ?
        ''', (history_json, phone_number))
    else:
        c.execute('''
            INSERT INTO conversations (phone_number, conversation_history, last_updated)
            VALUES (?, ?, CURRENT_TIMESTAMP)
        ''', (phone_number, history_json))

    conn.commit()
    conn.close()


def save_pending_query(query_id: str, customer_phone: str, question: str):
    """Guarda la consulta pendiente en la base de datos"""
    conn = sqlite3.connect('chatbot.db')
    c = conn.cursor()
    c.execute('''
        INSERT INTO pending_queries (query_id, customer_phone, question)
        VALUES (?, ?, ?)
    ''', (query_id, customer_phone, question))
    conn.commit()
    conn.close()


def is_price_query_by_model(text: str) -> bool:
    """Usa el modelo para determinar si es una consulta de precio"""
    try:
        headers = {
            "Content-Type": "application/json",
            "api-key": AZURE_API_KEY
        }

        payload = {
            "messages": [
                {"role": "system", "content": PRICE_DETECTION_PROMPT},
                {"role": "user", "content": text}
            ],
            "temperature": 0,
            "max_tokens": 5,
        }

        url = f"{AZURE_ENDPOINT}/openai/deployments/{AZURE_DEPLOYMENT}/chat/completions?api-version={AZURE_API_VERSION}"
        response = requests.post(url, headers=headers, json=payload)

        if response.status_code != 200:
            # Si hay un error, por defecto no lo tratamos como consulta de precio
            return False

        answer = response.json()["choices"][0]["message"]["content"].strip().upper()
        return "SI" in answer

    except Exception as e:
        print(f"Error al evaluar consulta de precio: {str(e)}")
        return False


@app.post("/price-response")
async def handle_price_response(response: PriceResponse):
    """Endpoint para recibir respuestas de precios del departamento de ventas"""
    query_id = response.query_id

    if query_id in pending_price_queries:
        # Obtener información de la consulta
        query_info = pending_price_queries[query_id]
        customer_phone = query_info["customer_phone"]

        # Formatear la respuesta para el cliente
        answer = f"✅ *Respuesta sobre tu consulta de precio:*\n\n{response.price_info}\n\n_Información proporcionada por nuestro departamento de ventas. Si tienes más dudas, estamos a tu servicio._"

        # Actualizar historial de conversación
        conversation_history = get_conversation_history(customer_phone)
        conversation_history.append({"role": "assistant", "content": answer})
        update_conversation_history(customer_phone, conversation_history)

        # Eliminar la consulta del diccionario de pendientes
        del pending_price_queries[query_id]

        # También eliminar de la base de datos
        conn = sqlite3.connect('chatbot.db')
        c = conn.cursor()
        c.execute('DELETE FROM pending_queries WHERE query_id = ?', (query_id,))
        conn.commit()
        conn.close()

        return {
            "status": "success",
            "message": "Respuesta de precio procesada correctamente",
            "customer_phone": customer_phone,
            "answer": answer
        }

    return {
        "status": "error",
        "message": f"No se encontró la consulta con ID {query_id}"
    }


@app.post("/chat")
async def chat_endpoint(request: ChatRequest):
    try:
        conversation_history = get_conversation_history(request.phone_number)

        # Usar el modelo para determinar si es una consulta de precio
        if is_price_query_by_model(request.question):
            # Generar ID único para la consulta
            query_id = f"price_{uuid.uuid4().hex[:8]}"

            # Guardar la consulta en memoria y base de datos
            pending_price_queries[query_id] = {
                "customer_phone": request.phone_number,
                "question": request.question
            }
            save_pending_query(query_id, request.phone_number, request.question)

            # Mensaje para el departamento de ventas
            sales_message = f"""
*📲 CONSULTA DE PRECIO*
------------------
De: Cliente {request.phone_number}
Consulta: {request.question}
------------------
Para responder, envía:
#precio {query_id} [información del precio]
            """

            # Respuesta para el cliente
            client_response = "🕒 Estoy consultando esta información de precio con nuestro departamento de ventas. Te responderé tan pronto tenga la información exacta. Gracias por tu paciencia."

            # Actualizar historial
            conversation_history.append({"role": "user", "content": request.question})
            conversation_history.append({"role": "assistant", "content": client_response})
            update_conversation_history(request.phone_number, conversation_history)

            return {
                "answer": client_response,
                "status": "price_query",
                "forward_to": SALES_DEPARTMENT,
                "forward_message": sales_message,
                "query_id": query_id
            }

        # Procesamiento normal para preguntas no relacionadas con precios
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages.extend(conversation_history)
        messages.append({"role": "user", "content": request.question})

        headers = {
            "Content-Type": "application/json",
            "api-key": AZURE_API_KEY
        }

        payload = {
            "messages": messages,
            "temperature": 0.7,
            "max_tokens": 800,
            "top_p": 0.95,
            "frequency_penalty": 0,
            "presence_penalty": 0
        }

        url = f"{AZURE_ENDPOINT}/openai/deployments/{AZURE_DEPLOYMENT}/chat/completions?api-version={AZURE_API_VERSION}"

        response = requests.post(url, headers=headers, json=payload)

        if response.status_code != 200:
            raise HTTPException(status_code=response.status_code,
                                detail=f"Azure OpenAI API error: {response.text}")

        response_data = response.json()
        answer = response_data["choices"][0]["message"]["content"]

        conversation_history.append({"role": "user", "content": request.question})
        conversation_history.append({"role": "assistant", "content": answer})

        update_conversation_history(request.phone_number, conversation_history)

        return {
            "answer": answer,
            "status": "success"
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/conversation/{phone_number}")
async def get_conversation(phone_number: str):
    try:
        history = get_conversation_history(phone_number)
        return {
            "phone_number": phone_number,
            "conversation_history": history
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/pending-queries")
async def get_pending_queries():
    """Endpoint para ver todas las consultas pendientes"""
    return {
        "pending_queries": pending_price_queries
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)