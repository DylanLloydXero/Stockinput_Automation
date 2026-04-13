import os
import json
import shutil
import socket
import pandas as pd
from PIL import Image
from datetime import datetime
from fastapi import FastAPI, File, UploadFile, Request, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from google import genai
from google.genai import types
import qrcode

# Setup Gemini
CONFIG_PATH = "config.json"
GEMINI_KEY = ""
if os.path.exists(CONFIG_PATH):
    with open(CONFIG_PATH, 'r') as f:
        config = json.load(f)
        GEMINI_KEY = config.get("GEMINI_API_KEY", "")

client = None
if GEMINI_KEY:
    client = genai.Client(api_key=GEMINI_KEY)

app = FastAPI(title="Stockinput Manager")

# Directories
UPLOAD_DIR = "static/uploads"
LOG_DIR = "data/logs"
IMG_LOG_DIR = os.path.join(LOG_DIR, "images")
HISTORY_FILE = os.path.join(LOG_DIR, "history.json")
PENDING_FILE = os.path.join(LOG_DIR, "pending.json")
MASTER_STOCK_FILE = os.path.join(LOG_DIR, "master_stock.xlsx")

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(IMG_LOG_DIR, exist_ok=True)

# Generate QR Code for Local Access (Auto-Detect IP)
def generate_local_qr():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
    except:
        ip = "127.0.0.1"
        
    url = f"http://{ip}:8001"
    qr = qrcode.QRCode(version=1, box_size=10, border=5)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    img.save("static/qr_code.png")
    print(f"QR Code generated for {url}")
    return url

CURRENT_URL = generate_local_qr()

# Mount static files
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="static")

async def process_invoice_background(filename: str):
    """Background task to run AI extraction using Gemini 2.0 Flash."""
    img_path = os.path.join(UPLOAD_DIR, filename)
    if not client or not os.path.exists(img_path):
        return

    try:
        img = Image.open(img_path)
        
        prompt = """
        Analyze this invoice and extract ALL stock items.
        Format the output as a JSON object with these exact keys:
        - supplier: Name of the vendor
        - total_amount: Grand total of the invoice
        - items: A list of objects with:
            - description: Clean item name
            - qty: Quantity appearing on invoice
            - unit: Units (e.g. 'kg', 'case', 'units')
            - unit_price: Cost per unit
            - total_price: Line total
        Return ONLY the JSON object.
        """
        
        response = client.models.generate_content(
            model="gemini-flash-latest",
            contents=[prompt, img]
        )
        
        text = response.text.strip()
        if "```json" in text: text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text: text = text.split("```")[1].split("```")[0].strip()
        
        extraction = json.loads(text)
        
        pending = {}
        if os.path.exists(PENDING_FILE):
            with open(PENDING_FILE, 'r') as f: pending = json.load(f)
        
        pending[filename] = {
            "status": "ready",
            "data": extraction,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M")
        }
        with open(PENDING_FILE, 'w') as f: json.dump(pending, f, indent=2)
            
    except Exception as e:
        print(f"Background Extraction Error: {e}")
        pending = {}
        if os.path.exists(PENDING_FILE):
            with open(PENDING_FILE, 'r') as f: pending = json.load(f)
        pending[filename] = {"status": "failed", "error": str(e)}
        with open(PENDING_FILE, 'w') as f: json.dump(pending, f, indent=2)

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    return templates.TemplateResponse(request=request, name="index.html", context={"url": CURRENT_URL})

@app.post("/upload")
async def upload_file(background_tasks: BackgroundTasks, file: UploadFile = File(...)):
    try:
        ext = file.filename.split('.')[-1]
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"invoice_{timestamp}.{ext}"
        
        temp_path = os.path.join(UPLOAD_DIR, filename)
        log_path = os.path.join(IMG_LOG_DIR, filename)
        
        with open(temp_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        shutil.copy(temp_path, log_path)
        
        pending = {}
        if os.path.exists(PENDING_FILE):
            with open(PENDING_FILE, 'r') as f: pending = json.load(f)
        pending[filename] = {"status": "processing", "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M")}
        with open(PENDING_FILE, 'w') as f: json.dump(pending, f, indent=2)

        background_tasks.add_task(process_invoice_background, filename)
        return JSONResponse(content={"message": "Upload successful", "filename": filename})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.get("/api/pending/{filename}")
async def get_pending_item(filename: str):
    if not os.path.exists(PENDING_FILE):
        raise HTTPException(status_code=404, detail="No pending items")
    with open(PENDING_FILE, 'r') as f:
        pending = json.load(f)
    if filename not in pending:
        raise HTTPException(status_code=404, detail="Item not found")
    return JSONResponse(content=pending[filename])

@app.post("/api/confirm")
async def confirm_extraction(request: Request):
    try:
        data = await request.json()
        filename = data.get("filename")
        extraction = data.get("extraction")
        
        history = []
        if os.path.exists(HISTORY_FILE):
            with open(HISTORY_FILE, 'r') as f: history = json.load(f)
        
        entry = {
            "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "supplier": extraction.get("supplier"),
            "item_count": len(extraction.get("items", [])),
            "total_value": extraction.get("total_amount"),
            "filename": filename
        }
        history.append(entry)
        with open(HISTORY_FILE, 'w') as f: json.dump(history, f, indent=2)
            
        items_df = pd.DataFrame(extraction.get("items", []))
        items_df['Supplier'] = extraction.get("supplier")
        items_df['Invoice_Date'] = entry["date"]
        items_df['Image'] = filename
        
        if os.path.exists(MASTER_STOCK_FILE):
            existing_df = pd.read_excel(MASTER_STOCK_FILE)
            final_df = pd.concat([existing_df, items_df], ignore_index=True)
        else:
            final_df = items_df
        final_df.to_excel(MASTER_STOCK_FILE, index=False)

        if os.path.exists(PENDING_FILE):
            with open(PENDING_FILE, 'r') as f: pending = json.load(f)
            if filename in pending:
                del pending[filename]
                with open(PENDING_FILE, 'w') as f: json.dump(pending, f, indent=2)
            
        return JSONResponse(content={"message": "Data pushed successfully"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.get("/api/logs")
async def get_logs():
    history = []
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, 'r') as f: history = json.load(f)
    pending = {}
    if os.path.exists(PENDING_FILE):
        with open(PENDING_FILE, 'r') as f: pending = json.load(f)
    return JSONResponse(content={"history": history, "pending": pending})

@app.post("/api/clear_pending")
async def clear_pending():
    if os.path.exists(PENDING_FILE):
        with open(PENDING_FILE, 'w') as f: json.dump({}, f)
    return JSONResponse(content={"message": "Cleared"})

@app.get("/api/export")
async def export_logs():
    if os.path.exists(MASTER_STOCK_FILE):
        return FileResponse(MASTER_STOCK_FILE, filename="Stock_Export.xlsx")
    raise HTTPException(status_code=404, detail="No exported data available")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
