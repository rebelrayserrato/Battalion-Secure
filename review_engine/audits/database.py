from __future__ import annotations
import json, sqlite3, uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from review_engine.config.settings import DATABASE_PATH, ensure_directories
from review_engine.extraction.models import SourceChunk

class ReviewDatabase:
 def __init__(self,path=DATABASE_PATH):
  ensure_directories(); self.path=Path(path); self.path.parent.mkdir(parents=True,exist_ok=True); self.initialize()
 @contextmanager
 def connect(self):
  db=sqlite3.connect(self.path); db.row_factory=sqlite3.Row
  try: yield db; db.commit()
  finally: db.close()
 def initialize(self):
  with self.connect() as d: d.executescript('''CREATE TABLE IF NOT EXISTS matters(id TEXT PRIMARY KEY,name TEXT,description TEXT,jurisdiction TEXT,created_at TEXT); CREATE TABLE IF NOT EXISTS documents(id INTEGER PRIMARY KEY,matter_id TEXT,name TEXT,path TEXT,file_type TEXT,size INTEGER,uploaded_at TEXT,processed_at TEXT,UNIQUE(matter_id,name)); CREATE TABLE IF NOT EXISTS chunks(source_ref TEXT PRIMARY KEY,matter_id TEXT,document_name TEXT,file_type TEXT,page INTEGER,row_number INTEGER,section TEXT,text TEXT); CREATE TABLE IF NOT EXISTS entities(id INTEGER PRIMARY KEY,matter_id TEXT,entity_type TEXT,value TEXT,source_ref TEXT); CREATE TABLE IF NOT EXISTS findings(id INTEGER PRIMARY KEY,matter_id TEXT,title TEXT,category TEXT,explanation TEXT,sources_json TEXT,confidence TEXT,confidence_reason TEXT,human_review_required INTEGER,created_at TEXT); CREATE TABLE IF NOT EXISTS audit_logs(id INTEGER PRIMARY KEY,matter_id TEXT,event_type TEXT,details TEXT,timestamp TEXT);''')
 @staticmethod
 def now(): return datetime.now(timezone.utc).isoformat()
 def log(self,event_type,matter_id=None,details=''):
  with self.connect() as d: d.execute('INSERT INTO audit_logs(matter_id,event_type,details,timestamp) VALUES(?,?,?,?)',(matter_id,event_type,details,self.now()))
 def create_matter(self,name,description='',jurisdiction=''):
  mid='MAT-'+uuid.uuid4().hex[:10].upper()
  with self.connect() as d: d.execute('INSERT INTO matters VALUES(?,?,?,?,?)',(mid,name.strip(),description.strip(),jurisdiction.strip(),self.now()))
  self.log('matter_created',mid,name); return mid
 def list_matters(self):
  with self.connect() as d: return [dict(r) for r in d.execute('SELECT * FROM matters ORDER BY created_at DESC')]
 def get_matter(self,mid):
  with self.connect() as d:
   r=d.execute('SELECT * FROM matters WHERE id=?',(mid,)).fetchone(); return dict(r) if r else None
 def add_document(self,mid,name,path):
  path=Path(path)
  with self.connect() as d: d.execute('INSERT OR REPLACE INTO documents(matter_id,name,path,file_type,size,uploaded_at) VALUES(?,?,?,?,?,?)',(mid,name,str(path),path.suffix.lower(),path.stat().st_size,self.now()))
  self.log('file_uploaded',mid,name)
 def list_documents(self,mid):
  with self.connect() as d: return [dict(r) for r in d.execute('SELECT * FROM documents WHERE matter_id=? ORDER BY name',(mid,))]
 def replace_document_chunks(self,mid,name,chunks):
  chunks=list(chunks)
  with self.connect() as d:
   d.execute('DELETE FROM chunks WHERE matter_id=? AND document_name=?',(mid,name)); d.executemany('INSERT INTO chunks VALUES(?,?,?,?,?,?,?,?)',[(c.source_ref,c.matter_id,c.document_name,c.file_type,c.page,c.row,c.section,c.text) for c in chunks]); d.execute('UPDATE documents SET processed_at=? WHERE matter_id=? AND name=?',(self.now(),mid,name))
  self.log('file_processed',mid,f'{name}: {len(chunks)} chunks')
 def get_chunks(self,mid):
  with self.connect() as d: rows=list(d.execute('SELECT * FROM chunks WHERE matter_id=?',(mid,)))
  return [SourceChunk(r['matter_id'],r['document_name'],r['file_type'],r['text'],r['source_ref'],r['page'],r['row_number'],r['section']) for r in rows]
 def replace_entities(self,mid,items):
  with self.connect() as d: d.execute('DELETE FROM entities WHERE matter_id=?',(mid,)); d.executemany('INSERT INTO entities(matter_id,entity_type,value,source_ref) VALUES(?,?,?,?)',[(mid,e['entity_type'],e['value'],e['source_ref']) for e in items])
 def get_entities(self,mid):
  with self.connect() as d: return [dict(r) for r in d.execute('SELECT * FROM entities WHERE matter_id=?',(mid,))]
 def replace_findings(self,mid,items):
  with self.connect() as d: d.execute('DELETE FROM findings WHERE matter_id=?',(mid,)); d.executemany('INSERT INTO findings(matter_id,title,category,explanation,sources_json,confidence,confidence_reason,human_review_required,created_at) VALUES(?,?,?,?,?,?,?,?,?)',[(mid,f['title'],f['category'],f['explanation'],json.dumps(f['supporting_sources']),f['confidence'],f['confidence_reason'],int(f['human_review_required']),self.now()) for f in items])
 def get_findings(self,mid):
  with self.connect() as d: rows=list(d.execute('SELECT * FROM findings WHERE matter_id=?',(mid,)))
  out=[]
  for r in rows:
   x=dict(r); x['supporting_sources']=json.loads(x.pop('sources_json')); x['human_review_required']=bool(x['human_review_required']); out.append(x)
  return out
 def get_audit_log(self,mid):
  with self.connect() as d: return [dict(r) for r in d.execute('SELECT * FROM audit_logs WHERE matter_id=? ORDER BY timestamp DESC',(mid,))]
