import os
import json
import shutil
import socket
import zipfile
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
from fpdf import FPDF
import asyncio

# Setup Gemini
CONFIG_PATH = "config.json"
GEMINI_KEY = ""
if os.path.exists(CONFIG_PATH):
    with open(CONFIG_PATH, 'r') as f:
        config = json.load(f)
        GEMINI_KEY = config.get("GEMINI_API_KEY", "")
    print(f"DEBUG: Using API Key starting with: {GEMINI_KEY[:5]}...")

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
os.makedirs("data", exist_ok=True)

# Generate QR Code
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
    return url

CURRENT_URL = generate_local_qr()

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="static")

# --- EXPORT ENDPOINTS ---
@app.get("/api/export/zip")
async def export_all_zip():
    zip_filename = f"Stock_Data_Backup_{datetime.now().strftime('%Y%m%d')}.zip"
    zip_path = os.path.join("data", zip_filename)
    try:
        with zipfile.ZipFile(zip_path, 'w') as z:
            if os.path.exists(MASTER_STOCK_FILE): z.write(MASTER_STOCK_FILE, os.path.basename(MASTER_STOCK_FILE))
            if os.path.exists(HISTORY_FILE): z.write(HISTORY_FILE, os.path.basename(HISTORY_FILE))
            for img in os.listdir(UPLOAD_DIR):
                img_path = os.path.join(UPLOAD_DIR, img)
                if os.path.isfile(img_path): z.write(img_path, os.path.join("images", img))
        return FileResponse(zip_path, filename=zip_filename)
    except Exception as e: return JSONResponse(status_code=500, content={"error": str(e)})

@app.get("/api/export/pdf")
async def export_summary_pdf():
    try:
        if not os.path.exists(HISTORY_FILE): raise HTTPException(status_code=404, detail="No history found")
        with open(HISTORY_FILE, 'r') as f: history = json.load(f)
        pdf = FPDF()
        pdf.add_page()
        pdf.set_font("Helvetica", "B", 20)
        pdf.cell(0, 15, "Stockinput Manager - Summary Report", ln=True, align="C")
        pdf.set_font("Helvetica", "", 10)
        pdf.cell(0, 10, f"Generated on: {datetime.now().strftime('%Y-%m-%d %H:%M')}", ln=True, align="C")
        pdf.ln(10)
        grouped = {}
        for h in history:
            s = h.get("supplier", "Unknown").upper()
            if s not in grouped: grouped[s] = []
            grouped[s].append(h)
        total_val = 0
        for supplier, logs in sorted(grouped.items()):
            pdf.set_font("Helvetica", "B", 14)
            pdf.set_fill_color(240, 240, 240)
            pdf.cell(0, 10, f"  {supplier}", ln=True, fill=True)
            pdf.set_font("Helvetica", "B", 10)
            pdf.cell(40, 8, "Date", 1); pdf.cell(80, 8, "Filename", 1); pdf.cell(30, 8, "Items", 1); pdf.cell(40, 8, "Total", 1, ln=True)
            pdf.set_font("Helvetica", "", 10)
            for h in sorted(logs, key=lambda x: x['date'], reverse=True):
                val = float(str(h.get('total_value', 0)).replace('R','').replace(',',''))
                total_val += val
                pdf.cell(40, 8, h['date'], 1); pdf.cell(80, 8, h.get('filename', ''), 1); pdf.cell(30, 8, f"{h.get('item_count', 0)}", 1); pdf.cell(40, 8, f"R{val:,.2f}", 1, ln=True)
            pdf.ln(5)
        pdf.ln(10); pdf.set_font("Helvetica", "B", 12)
        pdf.cell(0, 10, f"GRAND TOTAL: R{total_val:,.2f}", ln=True, align="R")
        output_path = "data/summary_report.pdf"
        pdf.output(output_path)
        return FileResponse(output_path, filename="Stock_Summary_Report.pdf")
    except Exception as e: return JSONResponse(status_code=500, content={"error": str(e)})

@app.get("/api/export")
async def export_logs():
    try:
        all_item_rows = []
        summary_rows = []

        # --- SOURCE 1: Confirmed items from master_stock.xlsx (via history) ---
        if os.path.exists(HISTORY_FILE) and os.path.exists(MASTER_STOCK_FILE):
            with open(HISTORY_FILE, 'r') as f:
                history = json.load(f)
            current_hist = [h for h in history if not h.get("archived")]
            current_files = set(h.get("filename") for h in current_hist)

            master_df = pd.read_excel(MASTER_STOCK_FILE)
            if 'Image' in master_df.columns and not master_df.empty:
                confirmed_rows = master_df[master_df['Image'].isin(current_files)]
                all_item_rows.append(confirmed_rows)

            for h in current_hist:
                summary_rows.append({
                    "Status": "Confirmed",
                    "Date": h.get("date", ""),
                    "Supplier": h.get("supplier", ""),
                    "Items": h.get("item_count", 0),
                    "Total (R)": h.get("total_value", 0),
                    "Invoice File": h.get("filename", "")
                })

        # --- SOURCE 2: Ready-for-review items still in pending (not yet confirmed) ---
        if os.path.exists(PENDING_FILE):
            with open(PENDING_FILE, 'r') as f:
                pending = json.load(f)
            for filename, info in pending.items():
                if info.get("status") != "ready": continue
                extraction = info.get("data", {})
                items = extraction.get("items", [])
                supplier = extraction.get("supplier", "")
                date_str = info.get("timestamp", datetime.now().strftime("%Y-%m-%d %H:%M"))
                if items:
                    pend_df = pd.DataFrame(items)
                    pend_df['Supplier'] = supplier
                    pend_df['Invoice_Date'] = date_str
                    pend_df['Image'] = filename
                    all_item_rows.append(pend_df)
                summary_rows.append({
                    "Status": "Pending Review",
                    "Date": date_str,
                    "Supplier": supplier,
                    "Items": len(items),
                    "Total (R)": extraction.get("total_amount", 0),
                    "Invoice File": filename
                })

        if not all_item_rows and not summary_rows:
            raise HTTPException(status_code=404, detail="No finalized or ready stock data found")

        # --- BUILD SHEETS ---
        sheets = {}

        # Sheet 1: Invoice Summary
        if summary_rows:
            sheets["Invoice Summary"] = pd.DataFrame(summary_rows)

        # Sheet 2: All Line Items (raw)
        if all_item_rows:
            detail_df = pd.concat(all_item_rows, ignore_index=True)
            # Normalise column names
            col_map = {'description': 'Description', 'qty': 'Qty', 'unit': 'Unit',
                       'unit_price': 'Unit Price (R)', 'total_price': 'Total (R)',
                       'Supplier': 'Supplier', 'Invoice_Date': 'Date', 'Image': 'Invoice File'}
            detail_df = detail_df.rename(columns={k: v for k, v in col_map.items() if k in detail_df.columns})
            sheets["All Line Items"] = detail_df

            # Sheet 3: Grouped Products — same product + supplier + unit price → summed qty & total
            group_cols = ['Description', 'Supplier', 'Unit', 'Unit Price (R)']
            available = [c for c in group_cols if c in detail_df.columns]
            if available and 'Qty' in detail_df.columns:
                try:
                    # Robust number parsing
                    def clean_num(val):
                        if pd.isna(val) or val == '': return 0
                        # Handle potential list or dict (shouldn't happen but safe)
                        if isinstance(val, (list, dict)): return 0
                        s = str(val).replace('R','').replace(',','').replace(' ','').strip()
                        try: return float(s)
                        except: return 0

                    detail_df['Qty'] = detail_df['Qty'].apply(clean_num)
                    detail_df['Total (R)'] = detail_df['Total (R)'].apply(clean_num)
                    detail_df['Unit Price (R)'] = detail_df['Unit Price (R)'].apply(clean_num)

                    # Normalise text to prevent minor differences splitting groups
                    grp_df = detail_df.copy()
                    
                    def aggressive_clean(val):
                        if not val: return "UNKNOWN"
                        s = str(val).upper().strip()
                        # Remove all special chars and spaces for matching
                        clean = "".join(c for c in s if c.isalnum())
                        # But keep some readability if possible? No, for the KEY we want it exact.
                        # We'll use a representative description for the display.
                        return clean if clean else "UNKNOWN"

                    def clean_supplier(val):
                        if not val: return "UNKNOWN"
                        s = str(val).upper().strip()
                        # Remove business entity suffixes
                        for suffix in ["PTY LTD", "PTY", "LTD", "LIMITED", "MANAGEMENT COMPANY", "SERVICES", "TRADING AS", "T/A"]:
                            s = s.replace(" " + suffix, "")
                        # Normalise common supplier names
                        if "ALBANY" in s: return "ALBANY"
                        if "DAIRYLAND" in s: return "DAIRYLAND"
                        if "BIDFOOD" in s: return "BIDFOOD"
                        if "FAMOUS BRANDS" in s: return "FAMOUS BRANDS"
                        if "VEG & MORE" in s: return "VEG & MORE"
                        if "PNP" in s or "PICK N PAY" in s: return "PICK N PAY"
                        return s.strip()

                    # Create matching keys
                    grp_df['Match_Desc'] = grp_df['Description'].apply(aggressive_clean)
                    grp_df['Match_Supplier'] = grp_df['Supplier'].apply(clean_supplier)
                    grp_df['Match_Unit'] = grp_df['Unit'].apply(aggressive_clean)
                    
                    # Group by the matched keys
                    match_cols = ['Match_Desc', 'Match_Supplier', 'Match_Unit', 'Unit Price (R)']
                    grouped = (grp_df.groupby(match_cols, as_index=False)
                               .agg({
                                   'Qty': 'sum', 
                                   'Total (R)': 'sum',
                                   'Description': 'first', # Keep the first readable description
                                   'Supplier': 'first',    # Keep the first readable supplier
                                   'Unit': 'first'         # Keep the first readable unit
                               }))
                    
                    # Reorder and cleanup for display
                    final_cols = ['Description', 'Supplier', 'Unit', 'Unit Price (R)', 'Qty', 'Total (R)']
                    grouped = grouped[final_cols]
                    grouped = grouped.sort_values(['Supplier', 'Description'])
                    grouped['Total (R)'] = grouped['Total (R)'].round(2)
                    sheets["Grouped Products"] = grouped
                except Exception as ge:
                    print(f"Grouping error: {ge}")

        temp_export = "data/current_stock_export.xlsx"
        with pd.ExcelWriter(temp_export, engine='openpyxl') as writer:
            for sheet_name, df in sheets.items():
                df.to_excel(writer, sheet_name=sheet_name, index=False)

        fname = f"Stock_Report_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
        print(f"DEBUG: [EXPORT] Exported {len(summary_rows)} invoices, {sum(len(r) for r in all_item_rows)} line items")
        return FileResponse(temp_export, filename=fname)
    except HTTPException:
        raise
    except Exception as e:
        print(f"Export Error: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})

# --- Logic ---
@app.post("/api/retry/{filename}")
async def retry_extraction(filename: str, background_tasks: BackgroundTasks):
    try:
        print(f"DEBUG: Manual retry requested for {filename}")
        pending = {}
        if os.path.exists(PENDING_FILE):
            with open(PENDING_FILE, 'r') as f: pending = json.load(f)
        if filename in pending:
            pending[filename]["status"] = "processing"
            pending[filename]["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            pending[filename]["auto_retries"] = 0 # Reset on manual retry
            if "error" in pending[filename]: del pending[filename]["error"]
            with open(PENDING_FILE, 'w') as f: json.dump(pending, f, indent=2)
            
            # Use create_task to start immediately or add_task
            background_tasks.add_task(process_invoice_background, filename)
            print(f"DEBUG: Background task added for {filename}")
            return JSONResponse(content={"message": "Retry started successfully"})
        print(f"DEBUG: {filename} not found in pending")
        return JSONResponse(status_code=404, content={"error": "Not found in pending queue"})
    except Exception as e: 
        print(f"DEBUG: Retry error: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.post("/api/history/archive")
async def archive_history_item(request: Request):
    try:
        data = await request.json()
        filename = data.get("filename")
        if os.path.exists(HISTORY_FILE):
            with open(HISTORY_FILE, 'r') as f: history = json.load(f)
            for h in history:
                if h.get("filename") == filename: h["archived"] = True; break
            with open(HISTORY_FILE, 'w') as f: json.dump(history, f, indent=2)
        return JSONResponse(content={"success": True})
    except Exception as e: return JSONResponse(status_code=500, content={"error": str(e)})

@app.post("/api/history/delete")
async def delete_history_item(request: Request):
    try:
        data = await request.json()
        filename = data.get("filename")
        if os.path.exists(HISTORY_FILE):
            with open(HISTORY_FILE, 'r') as f: history = json.load(f)
            history = [h for h in history if h.get("filename") != filename]
            with open(HISTORY_FILE, 'w') as f: json.dump(history, f, indent=2)
        img_path = os.path.join(UPLOAD_DIR, filename)
        if os.path.exists(img_path): os.remove(img_path)
        return JSONResponse(content={"success": True})
    except Exception as e: return JSONResponse(status_code=500, content={"error": str(e)})

def save_items_to_excel(filename: str, items: list, supplier: str, date_str: str):
    """Save items to master Excel, replacing any existing rows for this filename."""
    try:
        items_df = pd.DataFrame(items)
        items_df['Supplier'] = supplier
        items_df['Invoice_Date'] = date_str
        items_df['Image'] = filename
        if os.path.exists(MASTER_STOCK_FILE):
            existing_df = pd.read_excel(MASTER_STOCK_FILE)
            # Remove any old rows for this filename before writing new ones
            existing_df = existing_df[existing_df['Image'] != filename]
            final_df = pd.concat([existing_df, items_df], ignore_index=True)
        else:
            final_df = items_df
        final_df.to_excel(MASTER_STOCK_FILE, index=False)
    except Exception as ex:
        print(f"Excel Error for {filename}: {ex}")

async def process_invoice_background(filename: str):
    img_path = os.path.join(UPLOAD_DIR, filename)
    print(f"DEBUG: [TASK START] Processing {filename}")
    
    try:
        if not client or not os.path.exists(img_path):
            error_msg = "API Client not initialized" if not client else f"Image file not found: {filename}"
            print(f"DEBUG: [FATAL] {error_msg}")
            
            pending = {}
            if os.path.exists(PENDING_FILE):
                with open(PENDING_FILE, 'r') as f: pending = json.load(f)
            if filename in pending:
                pending[filename].update({"status": "failed", "error": error_msg})
                with open(PENDING_FILE, 'w') as f: json.dump(pending, f, indent=2)
            return
        
        # Priority list of models (Updated for Gemini 2.5/3.x availability)
        models_to_try = ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-flash-latest", "gemini-pro-latest"]
        max_retries = 2
        
        for model_name in models_to_try:
            for attempt in range(max_retries):
                try:
                    print(f"DEBUG: [AI START] Model: {model_name}, Attempt: {attempt+1}")
                    img = Image.open(img_path)
                    prompt = (
                        "You are a stock management assistant. Carefully analyze this invoice image.\n\n"
                        "STEP 1 - SUPPLIER: Look at the very top of the invoice for the store/company name "
                        "(e.g. 'Pick n Pay', 'Checkers', 'Coca-Cola'). Do NOT confuse it with the customer/delivery address.\n\n"
                        "STEP 2 - ITEMS: Extract EVERY SINGLE line item listed on the invoice. "
                        "DO NOT skip any items. DO NOT summarise. If there are 20 items, return all 20. "
                        "For each item, extract: description, qty (number), unit (KG/Each/L/Ctn etc), unit_price (number), total_price (number).\n\n"
                        "STEP 3 - TOTAL: Extract the final total amount from the invoice.\n\n"
                        "Return ONLY valid JSON in this exact format, no extra text:\n"
                        "{\"supplier\": \"...\", \"total_amount\": 0.00, \"items\": ["
                        "{\"description\": \"...\", \"qty\": 1, \"unit\": \"Each\", \"unit_price\": 0.00, \"total_price\": 0.00}"
                        "]}"
                    )
                    
                    response = client.models.generate_content(model=model_name, contents=[prompt, img])
                    text = response.text.strip()
                    print(f"DEBUG: [AI RESPONSE] Received {len(text)} characters from {model_name}")
                    
                    if "```json" in text: text = text.split("```json")[1].split("```")[0].strip()
                    elif "```" in text: text = text.split("```")[1].split("```")[0].strip()
                    
                    extraction = json.loads(text)
                    print(f"DEBUG: [PARSE SUCCESS] Extraction complete for {filename}")
                    
                    # Ensure all items have qty and unit
                    for item in extraction.get("items", []):
                        if not item.get("qty"): item["qty"] = 1
                        if not item.get("unit"): item["unit"] = "unit"
                    
                    # Re-read pending file to minimize race conditions
                    pending = {}
                    if os.path.exists(PENDING_FILE):
                        with open(PENDING_FILE, 'r') as f: pending = json.load(f)
                    
                    pending[filename] = {
                        "status": "ready", 
                        "data": extraction, 
                        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"), 
                        "model_used": model_name,
                        "auto_retries": pending.get(filename, {}).get("auto_retries", 0)
                    }
                    with open(PENDING_FILE, 'w') as f: json.dump(pending, f, indent=2)
                    print(f"DEBUG: [TASK SUCCESS] {filename} is ready")
                    return # SUCCESS!
                    
                except Exception as e:
                    err_msg = str(e)
                    print(f"DEBUG: [AI ERROR] {model_name}: {err_msg}")
                    if ("429" in err_msg or "RESOURCE_EXHAUSTED" in err_msg):
                        if attempt < max_retries - 1:
                            print(f"DEBUG: [RETRY] Quota limit hit, waiting 5s...")
                            await asyncio.sleep(5)
                            continue
                        else:
                            print(f"DEBUG: [FALLBACK] Model {model_name} exhausted, trying next...")
                            break 
                    else:
                        # Non-quota error, still try to update status
                        raise e # Re-raise to catch-all handler

        # If we get here, all models failed
        raise Exception("All AI models exhausted or quota reached.")

    except Exception as outer_e:
        err_msg = str(outer_e)
        print(f"DEBUG: [TASK FAILED] {filename}: {err_msg}")
        try:
            pending = {}
            if os.path.exists(PENDING_FILE):
                with open(PENDING_FILE, 'r') as f: pending = json.load(f)
            
            current_retries = pending.get(filename, {}).get("auto_retries", 0)
            pending[filename].update({
                "status": "failed", 
                "error": err_msg,
                "auto_retries": current_retries
            })
            with open(PENDING_FILE, 'w') as f: json.dump(pending, f, indent=2)
            print(f"DEBUG: [STATUS UPDATED] {filename} set to failed")
        except Exception as file_e:
            print(f"DEBUG: [CRITICAL ERROR] Could not update pending file: {file_e}")

async def auto_retry_worker():
    """Background task that periodically retries failed items and recovers stuck ones."""
    print("DEBUG: Auto-retry worker started.")
    max_auto_attempts = 3
    while True:
        try:
            await asyncio.sleep(300) # Check every 5 minutes
            if os.path.exists(PENDING_FILE):
                with open(PENDING_FILE, 'r') as f: pending = json.load(f)
                
                changed = False
                now = datetime.now()
                
                for filename, info in pending.items():
                    status = info.get("status")
                    
                    # 1. Recover Stuck Tasks (Processing for > 10 mins)
                    if status == "processing":
                        try:
                            ts_str = info.get("timestamp", "")
                            ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M")
                            if (now - ts).total_seconds() > 600: # 10 minutes
                                print(f"DEBUG: [RECOVERY] {filename} was stuck in processing. Resetting to failed.")
                                info["status"] = "failed"
                                info["error"] = "Task timed out or server restarted during processing."
                                changed = True
                        except: pass # Ignore date parsing errors
                    
                    # 2. Auto-Retry Failed Tasks
                    if status == "failed":
                        retries = info.get("auto_retries", 0)
                        if retries < max_auto_attempts:
                            print(f"DEBUG: [AUTO-RETRY] {filename} (Attempt {retries + 1}/{max_auto_attempts})")
                            info["status"] = "processing"
                            info["auto_retries"] = retries + 1
                            info["timestamp"] = now.strftime("%Y-%m-%d %H:%M")
                            if "error" in info: del info["error"]
                            changed = True
                            
                            # Save immediately and start task
                            with open(PENDING_FILE, 'w') as f: json.dump(pending, f, indent=2)
                            asyncio.create_task(process_invoice_background(filename))
                            break # Only one at a time to avoid quota spikes

                if changed:
                    with open(PENDING_FILE, 'w') as f: json.dump(pending, f, indent=2)
                    
        except Exception as e:
            print(f"DEBUG: [WORKER ERROR] {e}")

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(auto_retry_worker())
    asyncio.create_task(process_pending_on_startup())

async def process_pending_on_startup():
    """On startup, queue all items stuck in 'processing' state as background tasks."""
    await asyncio.sleep(2) # Wait for server to fully initialize
    if not os.path.exists(PENDING_FILE): return
    
    with open(PENDING_FILE, 'r') as f: pending = json.load(f)
    stuck = [fn for fn, info in pending.items() if info.get("status") == "processing"]
    
    if not stuck:
        print(f"DEBUG: [STARTUP] No pending items to process.")
        return
    
    print(f"DEBUG: [STARTUP] Found {len(stuck)} items in processing. Queuing them...")
    for i, filename in enumerate(stuck):
        # Stagger by 1 second each to avoid hammering the API all at once
        await asyncio.sleep(1)
        print(f"DEBUG: [STARTUP] Queuing {filename} ({i+1}/{len(stuck)})")
        asyncio.create_task(process_invoice_background(filename))

@app.post("/api/confirm_all")
async def confirm_all():
    """Bulk-approve all 'ready' items that the AI is confident about. Skip uncertain ones."""
    try:
        if not os.path.exists(PENDING_FILE):
            return JSONResponse(content={"approved": 0, "skipped": 0})
        
        with open(PENDING_FILE, 'r') as f: pending = json.load(f)
        
        history = []
        if os.path.exists(HISTORY_FILE):
            with open(HISTORY_FILE, 'r') as f: history = json.load(f)
        
        approved = 0
        skipped = 0
        to_remove = []
        
        for filename, info in pending.items():
            if info.get("status") != "ready": continue
            
            extraction = info.get("data", {})
            supplier = (extraction.get("supplier") or "").strip()
            items = extraction.get("items", [])
            total = extraction.get("total_amount", 0)
            
            # Check confidence: skip if missing supplier, no items, or zero total
            try: total_val = float(str(total).replace('R','').replace(',',''))
            except: total_val = 0
            
            is_uncertain = not supplier or len(items) == 0 or total_val == 0
            
            if is_uncertain:
                skipped += 1
                continue
            
            # Save to history
            entry = {
                "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "supplier": supplier,
                "item_count": len(items),
                "total_value": total,
                "filename": filename
            }
            history.append(entry)
            
            # Save to master Excel (replace existing rows for this filename)
            save_items_to_excel(filename, items, supplier, entry["date"])
            
            to_remove.append(filename)
            approved += 1
        
        # Save history and clean pending
        with open(HISTORY_FILE, 'w') as f: json.dump(history, f, indent=2)
        for fn in to_remove:
            if fn in pending: del pending[fn]
        with open(PENDING_FILE, 'w') as f: json.dump(pending, f, indent=2)
        
        print(f"DEBUG: [BULK APPROVE] Approved {approved}, Skipped {skipped}")
        return JSONResponse(content={"approved": approved, "skipped": skipped})
    except Exception as e:
        print(f"DEBUG: [BULK APPROVE ERROR] {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.post("/api/clear_stuck")
async def clear_stuck():
    """Emergency endpoint to force-reset stuck processing items."""
    try:
        if os.path.exists(PENDING_FILE):
            with open(PENDING_FILE, 'r') as f: pending = json.load(f)
            changed = False
            for filename, info in pending.items():
                if info.get("status") == "processing":
                    info["status"] = "failed"
                    info["error"] = "Manually cleared/reset."
                    changed = True
            if changed:
                with open(PENDING_FILE, 'w') as f: json.dump(pending, f, indent=2)
            return JSONResponse(content={"message": "All stuck items reset."})
        return JSONResponse(content={"message": "No pending file found."})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    return templates.TemplateResponse(request=request, name="index.html", context={"url": CURRENT_URL})

@app.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    """Save the image immediately. AI scanning is triggered separately."""
    try:
        ext = file.filename.split('.')[-1]
        filename = f"invoice_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.{ext}"
        temp_path = os.path.join(UPLOAD_DIR, filename)
        with open(temp_path, "wb") as buffer: shutil.copyfileobj(file.file, buffer)
        pending = {}
        if os.path.exists(PENDING_FILE):
            with open(PENDING_FILE, 'r') as f: pending = json.load(f)
        # Mark as 'queued' - no AI scan yet, just saved to disk
        pending[filename] = {"status": "queued", "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"), "auto_retries": 0}
        with open(PENDING_FILE, 'w') as f: json.dump(pending, f, indent=2)
        print(f"DEBUG: [UPLOAD] Saved {filename} (queued for AI)")
        return JSONResponse(content={"message": "Upload successful", "filename": filename})
    except Exception as e: return JSONResponse(status_code=500, content={"error": str(e)})

@app.post("/api/process_queued")
async def process_queued():
    """Start AI scanning for all queued (uploaded but not yet scanned) items."""
    try:
        if not os.path.exists(PENDING_FILE):
            return JSONResponse(content={"started": 0})
        with open(PENDING_FILE, 'r') as f: pending = json.load(f)
        queued = [fn for fn, info in pending.items() if info.get("status") == "queued"]
        for filename in queued:
            pending[filename]["status"] = "processing"
            pending[filename]["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        with open(PENDING_FILE, 'w') as f: json.dump(pending, f, indent=2)
        # Fire all tasks with 1 second stagger
        for i, filename in enumerate(queued):
            asyncio.get_event_loop().call_later(i * 1.0, lambda fn=filename: asyncio.create_task(process_invoice_background(fn)))
        print(f"DEBUG: [PROCESS_QUEUED] Starting AI scan for {len(queued)} items")
        return JSONResponse(content={"started": len(queued)})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.get("/api/pending/{filename}")
async def get_pending_item(filename: str):
    if not os.path.exists(PENDING_FILE): raise HTTPException(status_code=404, detail="No pending items")
    with open(PENDING_FILE, 'r') as f: pending = json.load(f)
    return JSONResponse(content=pending.get(filename, {}))

@app.post("/api/confirm")
async def confirm_extraction(request: Request):
    try:
        data = await request.json()
        filename, extraction = data.get("filename"), data.get("extraction")
        history = []
        if os.path.exists(HISTORY_FILE):
            with open(HISTORY_FILE, 'r') as f: history = json.load(f)
        entry = {"date": datetime.now().strftime("%Y-%m-%d %H:%M"), "supplier": extraction.get("supplier"), "item_count": len(extraction.get("items", [])), "total_value": extraction.get("total_amount"), "filename": filename}
        history.append(entry)
        with open(HISTORY_FILE, 'w') as f: json.dump(history, f, indent=2)
        
        # --- Save to Master Excel (replace, not append) ---
        try:
            items = extraction.get("items", [])
            if items:
                save_items_to_excel(filename, items, extraction.get("supplier", ""), entry["date"])
        except Exception as ex: print(f"Excel Error: {ex}")

        if os.path.exists(PENDING_FILE):
            with open(PENDING_FILE, 'r') as f: pending = json.load(f)
            if filename in pending: del pending[filename]
            with open(PENDING_FILE, 'w') as f: json.dump(pending, f, indent=2)
        return JSONResponse(content={"message": "Data pushed successfully"})
    except Exception as e: return JSONResponse(status_code=500, content={"error": str(e)})

@app.get("/api/logs")
async def get_logs():
    history, pending = [], {}
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, 'r') as f: history = json.load(f)
    if os.path.exists(PENDING_FILE):
        with open(PENDING_FILE, 'r') as f: pending = json.load(f)
    return JSONResponse(content={"history": history, "pending": pending})

@app.post("/api/clear_pending")
async def clear_pending():
    if os.path.exists(PENDING_FILE):
        with open(PENDING_FILE, 'w') as f: json.dump({}, f)
    return JSONResponse(content={"message": "Cleared"})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
