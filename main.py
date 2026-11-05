from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from io import BytesIO
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import uuid
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageOps, UnidentifiedImageError

from excel_engine import examine_xlsx, export_xlsx

BASE = Path(__file__).resolve().parent
DATA = Path(os.getenv('APP_DATA_DIR', str(BASE / 'data'))).resolve()
DATA.mkdir(parents=True, exist_ok=True)
(DATA / 'projects').mkdir(exist_ok=True)
PASSWORD = os.getenv('APP_PASSWORD', '')
SECRET_PATH = DATA / '.session_secret'
if SECRET_PATH.is_file():
    SECRET = SECRET_PATH.read_bytes()
else:
    SECRET = secrets.token_bytes(32)
    SECRET_PATH.write_bytes(SECRET)
    try: SECRET_PATH.chmod(0o600)
    except OSError: pass
DB = DATA / 'app.sqlite3'
app = FastAPI(title='열화상 점검 대장', version='1.0.0', docs_url=None, redoc_url=None)


def database():
    c=sqlite3.connect(DB,timeout=15)
    c.row_factory=sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    return c


def init_db():
    with database() as conn:
        conn.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, original_name TEXT NOT NULL,
            template_path TEXT NOT NULL, photo_sheet TEXT NOT NULL,
            panels_json TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS readings (
            project_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
            date TEXT NOT NULL, temperature REAL NOT NULL, photo_path TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(project_id,ordinal),
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        ''')

init_db()


def seed_sample_once():
    """Ship the user's original workbook as an immediately usable sample project."""
    marker=DATA / '.seed_initialized'
    if marker.exists(): return
    sample=BASE / 'sample' / '분전반리스트_원본.xlsx'
    if sample.is_file():
        with database() as conn:
            any_project=conn.execute('SELECT 1 FROM projects LIMIT 1').fetchone()
        if not any_project:
            original=sample.read_bytes()
            info=examine_xlsx(original)
            pid=uuid.uuid4().hex
            folder=DATA / 'projects' / pid
            folder.mkdir(parents=True, exist_ok=True)
            path=folder / 'template.xlsx'
            path.write_bytes(original)
            with database() as conn:
                conn.execute('INSERT INTO projects VALUES (?,?,?,?,?,?,?)',(
                    pid,'세종나성점','분전반리스트(세종나성).xlsx',str(path),
                    info['photo_sheet'],json.dumps(info['panels'],ensure_ascii=False),
                    datetime.now(timezone.utc).isoformat()))
    marker.write_text('seeded',encoding='utf-8')

seed_sample_once()


def sign_token():
    expiration=str(int((datetime.now(timezone.utc)+timedelta(days=30)).timestamp()))
    signature=hmac.new(SECRET, expiration.encode(),hashlib.sha256).hexdigest()
    return f'{expiration}.{signature}'


def is_valid_cookie(value):
    if not value or '.' not in value:return False
    expiration,signature=value.rsplit('.',1)
    if not expiration.isdigit() or int(expiration)<int(datetime.now(timezone.utc).timestamp()):return False
    expected=hmac.new(SECRET,expiration.encode(),hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected,signature)


def require_auth(request:Request):
    if PASSWORD and not is_valid_cookie(request.cookies.get('panel_session')):
        raise HTTPException(status_code=401,detail='로그인이 필요합니다.')


def get_project(project_id):
    with database() as conn:
        row=conn.execute('SELECT * FROM projects WHERE id=?',(project_id,)).fetchone()
    if not row: raise HTTPException(404,'현장을 찾을 수 없습니다.')
    return dict(row)


def public_project(row, with_panels=False):
    with database() as conn:
        entries=conn.execute('SELECT * FROM readings WHERE project_id=?',(row['id'],)).fetchall()
    readings={r['ordinal']: dict(r) for r in entries}
    all_panels=json.loads(row['panels_json'])
    count=0
    panels=[]
    for p in all_panels:
        reading=readings.get(p['ordinal'])
        measured=bool(reading and Path(reading['photo_path']).is_file())
        count+=bool(measured)
        if with_panels:
            panels.append({
                'ordinal':p['ordinal'],'name':p['name'],'floor':p['floor'],
                'location':p['location'],'sheet_title':p['sheet_title'],
                'photo_ratio':p['photo_ratio'], 'measured':measured,
                'date':reading['date'] if reading else '',
                'temperature':reading['temperature'] if reading else '',
                'updated_at':reading['updated_at'] if reading else None,
                'original_date':p['original_date'],
                'original_temperature':p['original_temperature'],
            })
    return {
        'id':row['id'],'name':row['name'],'original_name':row['original_name'],
        'created_at':row['created_at'],'total':len(all_panels), 'measured':count,
        **({'panels':panels} if with_panels else {})
    }

@app.get('/api/auth')
def auth_status(request:Request):
    return {'required':bool(PASSWORD),'authenticated':bool(not PASSWORD or is_valid_cookie(request.cookies.get('panel_session')))}

@app.post('/api/auth/login')
async def login(request:Request):
    body=await request.json()
    submitted=body.get('password','')
    if not PASSWORD or not hmac.compare_digest(str(submitted).encode(),PASSWORD.encode()):
        raise HTTPException(status_code=401,detail='비밀번호가 올바르지 않습니다.')
    response=JSONResponse({'ok':True})
    response.set_cookie('panel_session',sign_token(),max_age=30*86400,httponly=True,
                        secure=request.url.scheme=='https',samesite='strict',path='/')
    return response

@app.post('/api/auth/logout')
def logout():
    response=JSONResponse({'ok':True})
    response.delete_cookie('panel_session',path='/')
    return response

@app.get('/api/projects',dependencies=[Depends(require_auth)])
def list_projects():
    with database() as conn:
        rows=conn.execute('SELECT * FROM projects ORDER BY created_at DESC').fetchall()
    return {'projects':[public_project(dict(r)) for r in rows]}

@app.post('/api/projects',dependencies=[Depends(require_auth)])
async def upload_project(file:UploadFile=File(...), project_name:str=Form('')):
    filename=Path(file.filename or '').name
    if not filename.lower().endswith('.xlsx'):
        raise HTTPException(400,'xlsx 형식의 엑셀 파일만 업로드할 수 있습니다.')
    contents=await file.read(25*1024*1024+1)
    try:
        book=examine_xlsx(contents)
    except ValueError as e:
        raise HTTPException(400,str(e))
    except Exception:
        raise HTTPException(400,'엑셀 형식을 분석하지 못했습니다. 파일 상태를 확인해 주세요.')
    project_id=uuid.uuid4().hex
    project_dir=DATA / 'projects' / project_id
    project_dir.mkdir(parents=True)
    template_path=project_dir / 'template.xlsx'
    template_path.write_bytes(contents)
    created_at=datetime.now(timezone.utc).isoformat()
    name=project_name.strip()[:100] or Path(filename).stem[:100]
    with database() as conn:
        conn.execute('INSERT INTO projects VALUES (?,?,?,?,?,?,?)',(
            project_id,name,filename,str(template_path),book['photo_sheet'],
            json.dumps(book['panels'],ensure_ascii=False),created_at))
    return public_project(get_project(project_id),with_panels=True)

@app.get('/api/projects/{project_id}',dependencies=[Depends(require_auth)])
def project_detail(project_id:str):
    return public_project(get_project(project_id),with_panels=True)

@app.patch('/api/projects/{project_id}',dependencies=[Depends(require_auth)])
async def rename_project(project_id:str,request:Request):
    get_project(project_id)
    body=await request.json()
    name=str(body.get('name','')).strip()[:100]
    if not name:raise HTTPException(400,'현장명을 입력해 주세요.')
    with database() as conn:conn.execute('UPDATE projects SET name=? WHERE id=?',(name,project_id))
    return public_project(get_project(project_id))

@app.delete('/api/projects/{project_id}',dependencies=[Depends(require_auth)])
def delete_project(project_id:str):
    row=get_project(project_id)
    with database() as conn:conn.execute('DELETE FROM projects WHERE id=?',(project_id,))
    shutil.rmtree(DATA / 'projects' / project_id,ignore_errors=True)
    return {'ok':True}

@app.post('/api/projects/{project_id}/panels/{ordinal}',dependencies=[Depends(require_auth)])
async def save_reading(project_id:str,ordinal:int, temperature:float=Form(...),
                       check_date:str=Form(...),photo:UploadFile|None=File(None)):
    row=get_project(project_id)
    panels=json.loads(row['panels_json'])
    found=next((p for p in panels if p['ordinal']==ordinal),None)
    if not found: raise HTTPException(404,'판넬을 찾을 수 없습니다.')
    try: parsed=date.fromisoformat(check_date)
    except ValueError:raise HTTPException(400,'날짜가 올바르지 않습니다.')
    if not (2000<=parsed.year<=2100):raise HTTPException(400,'날짜 범위를 확인해 주세요.')
    if not (-273.15<=temperature<=2000) or temperature!=temperature:
        raise HTTPException(400,'온도는 -273.15~2000℃ 사이로 입력해 주세요.')
    temp_value=round(temperature,2)
    photo_path=DATA / 'projects' / project_id / f'{ordinal}.jpg'
    if photo is not None:
        raw=await photo.read(12*1024*1024+1)
        if len(raw)>12*1024*1024:raise HTTPException(400,'사진은 12MB 이하만 등록할 수 있습니다.')
        try:
            with Image.open(BytesIO(raw)) as original:
                img=ImageOps.exif_transpose(original).convert('RGB')
        except (UnidentifiedImageError, OSError, ValueError):
            raise HTTPException(400,'사진을 열 수 없습니다. JPG 또는 PNG 파일을 사용해 주세요.')
        ratio=found['photo_ratio']
        w,h=img.size
        if w*h>60_000_000:raise HTTPException(400,'사진 해상도가 너무 큽니다.')
        if w/h>ratio:
            new_w=max(1,round(h*ratio));left=(w-new_w)//2
            img=img.crop((left,0,left+new_w,h))
        else:
            new_h=max(1,round(w/ratio));top=(h-new_h)//2
            img=img.crop((0,top,w,top+new_h))
        if img.width>1400:img.thumbnail((1400,1400))
        tmp_path=photo_path.with_suffix('.tmp')
        img.save(tmp_path,format='JPEG',quality=90,optimize=True)
        os.replace(tmp_path,photo_path)
    if not photo_path.is_file():
        raise HTTPException(400,'먼저 사진을 촬영하거나 업로드해 주세요.')
    with database() as conn:
        conn.execute('''INSERT INTO readings (project_id, ordinal, date, temperature, photo_path, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(project_id,ordinal) DO UPDATE SET
                        date=excluded.date, temperature=excluded.temperature,
                        photo_path=excluded.photo_path, updated_at=excluded.updated_at''',(
            project_id,ordinal,check_date,temp_value,str(photo_path),datetime.now(timezone.utc).isoformat()))
    return {'ok':True,'ordinal':ordinal,'temperature':temp_value,'date':check_date}

@app.delete('/api/projects/{project_id}/panels/{ordinal}',dependencies=[Depends(require_auth)])
def clear_reading(project_id:str,ordinal:int):
    get_project(project_id)
    with database() as conn:
        conn.execute('DELETE FROM readings WHERE project_id=? AND ordinal=?',(project_id,ordinal))
    photo_path=DATA / 'projects' / project_id / f'{ordinal}.jpg'
    photo_path.unlink(missing_ok=True)
    return {'ok':True}

@app.get('/api/projects/{project_id}/panels/{ordinal}/photo',dependencies=[Depends(require_auth)])
def get_photo(project_id:str,ordinal:int):
    get_project(project_id)
    path=DATA / 'projects' / project_id / f'{ordinal}.jpg'
    if not path.is_file(): raise HTTPException(404,'사진이 없습니다.')
    return FileResponse(path,media_type='image/jpeg',headers={'Cache-Control':'no-store'})

@app.get('/api/projects/{project_id}/export',dependencies=[Depends(require_auth)])
def export_project(project_id:str):
    row=get_project(project_id)
    with database() as conn:
        reads={r['ordinal']:dict(r) for r in conn.execute('SELECT * FROM readings WHERE project_id=?',(project_id,))}
    panels=json.loads(row['panels_json'])
    data=export_xlsx(Path(row['template_path']).read_bytes(),panels,reads,row['photo_sheet'])
    safe_stem=Path(row['original_name']).stem[:100]
    filename=f'{safe_stem}_점검결과_{date.today().isoformat()}.xlsx'
    fallback=f'inspection_{date.today().isoformat()}.xlsx'
    return Response(data,media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',headers={
        'Content-Disposition':f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename)}",
        'Cache-Control':'no-store'
    })

@app.get('/')
def index():
    return FileResponse(BASE / 'static' / 'index.html',headers={'Cache-Control':'no-cache'})

app.mount('/static',StaticFiles(directory=str(BASE / 'static')),name='static')
