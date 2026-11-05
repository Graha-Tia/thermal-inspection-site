'use strict';

const $ = id => document.getElementById(id);
const state = {
  projects: [], project: null, projectId: null, floor: 'all', search: '', status: 'all',
  selected: null, currentStep: 1, croppedBlob: null, imageFile: null,
  image: null, objectUrl: null, previewUrl: null,
  zoom: 1, panX: 0, panY: 0, drag: null,
};

const escapeHTML = text => String(text ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function toast(text, error=false) {
  const node=document.createElement('div');node.className='toast'+(error?' error':'');
  node.textContent=text; $('toast-container').appendChild(node);
  setTimeout(()=>node.remove(), 3800);
}
async function api(url,opts={}) {
  const res=await fetch(url,{credentials:'same-origin',...opts});
  if(!res.ok) {
    let message='요청을 처리하지 못했습니다.';
    try { const data=await res.json();message=typeof data.detail==='string'?data.detail:message; } catch {}
    if(res.status===401) { $('app-layout').classList.add('hidden');$('auth-screen').classList.remove('hidden'); }
    throw new Error(message);
  }
  return res;
}
async function getJson(url,opts) {return (await api(url,opts)).json();}
function openModal(id) {$(id).classList.remove('hidden');document.body.style.overflow='hidden';}
function closeModal(id) {$(id).classList.add('hidden');if(document.querySelectorAll('.modal-backdrop:not(.hidden)').length===0)document.body.style.overflow='';}
function todayLocal() { const d=new Date();return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`; }

async function boot() {
  const auth=await getJson('/api/auth');
  if(!auth.authenticated) { $('auth-screen').classList.remove('hidden');return; }
  $('app-layout').classList.remove('hidden');
  $('auth-screen').classList.add('hidden');
  $('logout').classList.toggle('hidden',!auth.required);
  await loadProjects();
}
async function loadProjects(selectId) {
  const data=await getJson('/api/projects');state.projects=data.projects;
  if(selectId) state.projectId=selectId;
  if(!state.projects.find(p=>p.id===state.projectId))state.projectId=state.projects[0]?.id||null;
  renderSidebar();
  await loadCurrentProject();
}
function renderSidebar() {
  const container=$('sidebar-projects'); container.innerHTML='';
  const selector=$('project-select');selector.innerHTML='';
  state.projects.forEach(p=>{
    const opt=document.createElement('option');opt.value=p.id;opt.textContent=p.name;selector.appendChild(opt);
  });
  selector.value=state.projectId||'';selector.disabled=state.projects.length===0;
  state.projects.forEach(p=>{
    const btn=document.createElement('button');btn.className='side-project'+(state.projectId===p.id?' active':'');
    const span=document.createElement('span');span.textContent=p.name;btn.appendChild(span);
    btn.title=p.name;btn.addEventListener('click',()=>switchProject(p.id));container.appendChild(btn);
  });
}
async function switchProject(id) {
  if(id===state.projectId)return;
  state.projectId=id;state.floor='all';state.search='';state.status='all';
  $('search').value='';$('status-filter').value='all';
  renderSidebar();await loadCurrentProject();
}
async function loadCurrentProject() {
  if(!state.projectId){
    state.project=null;
    $('project-content').classList.add('hidden');$('empty-state').classList.remove('hidden');
    $('download-btn').disabled=true;$('rename-project').disabled=true;$('remove-project').disabled=true;
    $('breadcrumb-project').textContent='현장 선택';return;
  }
  state.project=await getJson(`/api/projects/${state.projectId}`);
  $('empty-state').classList.add('hidden');$('project-content').classList.remove('hidden');
  $('download-btn').disabled=false;$('rename-project').disabled=false;$('remove-project').disabled=false;
  $('breadcrumb-project').textContent=state.project.name;
  $('project-title').textContent=state.project.name;
  $('project-meta').textContent=`${state.project.original_name} · ${new Date(state.project.created_at).toLocaleDateString('ko-KR')} 등록`;
  $('total-count').innerHTML=`${state.project.total} <small>개</small>`;
  $('done-count').innerHTML=`${state.project.measured} <small>개</small>`;
  $('pending-count').innerHTML=`${state.project.total-state.project.measured} <small>개</small>`;
  const pct=state.project.total?Math.round(state.project.measured/state.project.total*100):0;
  $('progress-count').innerHTML=`${pct}<small>%</small>`;
  $('progress-fill').style.width=`${pct}%`;
  renderFloors();renderPanels();
}
function renderFloors() {
  const floors=[...new Set(state.project.panels.map(p=>p.floor))];
  if(state.floor!=='all'&&!floors.includes(state.floor))state.floor='all';
  const container=$('floor-filters');container.innerHTML='';
  for(const floor of ['all',...floors]) {
    const btn=document.createElement('button');btn.type='button';btn.className='floor-tab'+(state.floor===floor?' active':'');
    const count=floor==='all'?state.project.panels.length:state.project.panels.filter(p=>p.floor===floor).length;
    btn.textContent=(floor==='all'?'전체 층':floor)+` (${count})`;
    btn.addEventListener('click',()=>{state.floor=floor;renderFloors();renderPanels();});
    container.appendChild(btn);
  }
}
function renderPanels() {
  const query=state.search.trim().toLocaleLowerCase();
  const rows=state.project.panels.filter(p=>{
    if(state.floor!=='all'&&p.floor!==state.floor)return false;
    if(state.status==='done'&&!p.measured)return false;
    if(state.status==='pending'&&p.measured)return false;
    return !query || [p.name,p.location,p.floor,p.sheet_title,String(p.ordinal)].some(t=>t.toLocaleLowerCase().includes(query));
  });
  $('visible-count').textContent=`${rows.length}개 항목`;
  $('no-results').classList.toggle('hidden',rows.length!==0);
  const tbody=$('panels-body');tbody.innerHTML='';
  const mobile=$('mobile-panels');mobile.innerHTML='';
  for(const p of rows) {
    const tr=document.createElement('tr');tr.tabIndex=0;tr.setAttribute('role','button');tr.setAttribute('aria-label',`${p.name}, ${p.floor}, ${p.location}, ${p.measured?'측정완료':'미측정'}`);
    tr.innerHTML=`<td>${p.ordinal}</td><td class="panel-main">${escapeHTML(p.name)}<div class="panel-sub">${escapeHTML(p.sheet_title)}</div></td><td><span class="floor-chip">${escapeHTML(p.floor)}</span></td><td>${escapeHTML(p.location)}</td><td>${p.measured?`<span class="temp-display">${escapeHTML(p.temperature)}℃</span>`:'—'}</td><td><span class="reading-pill ${p.measured?'done':'pending'}">${p.measured?'측정 완료':'미측정'}</span></td><td class="table-arrow">›</td>`;
    tr.addEventListener('click',()=>openInspection(p.ordinal));
    tr.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();openInspection(p.ordinal);}});
    tbody.appendChild(tr);
    const card=document.createElement('button');card.type='button';card.className='mobile-panel';
    card.innerHTML=`<div class="mobile-panel-left"><span class="mobile-index">${p.ordinal}</span><div class="mobile-panel-text"><div class="mobile-panel-name">${escapeHTML(p.name)}</div><div class="mobile-panel-location">${escapeHTML(p.floor)} · ${escapeHTML(p.location)}</div></div></div><div class="mobile-panel-right"><span class="reading-pill ${p.measured?'done':'pending'}">${p.measured?'측정 완료':'미측정'}</span><span class="mobile-panel-temp">${p.measured?escapeHTML(p.temperature)+'℃':'온도 미입력'} ›</span></div>`;
    card.addEventListener('click',()=>openInspection(p.ordinal));mobile.appendChild(card);
  }
}
function startUpload() { $('upload-form').reset();$('file-drop-title').textContent='.xlsx 파일을 선택하세요';openModal('upload-modal'); }
async function submitUpload(event) {
  event.preventDefault();const file=$('upload-file').files[0];if(!file)return toast('엑셀 파일을 선택해 주세요.',true);
  const button=$('submit-upload');button.disabled=true;button.textContent='분석 중…';
  try{
    const fd=new FormData();fd.append('file',file);fd.append('project_name',$('upload-name').value);
    const project=await getJson('/api/projects',{method:'POST',body:fd});
    closeModal('upload-modal');state.floor='all';state.search='';state.status='all';
    $('search').value='';$('status-filter').value='all';
    await loadProjects(project.id);
    toast(`${project.name} · 측정 대상 ${project.total}개를 불러왔습니다.`);
  } catch(e){toast(e.message,true);}finally{button.disabled=false;button.textContent='현장 등록';}
}
async function downloadProject(){
  if(!state.projectId)return;
  const button=$('download-btn');button.disabled=true;button.textContent='엑셀 생성 중…';
  try{
    const resp=await api(`/api/projects/${state.projectId}/export`);
    const blob=await resp.blob();
    const disposition=resp.headers.get('content-disposition')||'';
    const match=disposition.match(/filename\*=UTF-8''([^;]+)/i);
    const filename=match?decodeURIComponent(match[1]):`${state.project.name}_점검결과.xlsx`;
    const url=URL.createObjectURL(blob);const a=document.createElement('a');a.href=url;a.download=filename;
    document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);
    toast('현재까지 저장된 측정 결과로 엑셀을 내려받았습니다.');
  }catch(e){toast(e.message,true);}finally{button.disabled=false;button.innerHTML='<span class="btn-icon">↓</span> 엑셀 다운로드';}
}

// Inspection step 1: choose image; step 2: crop to exact merged-cell aspect; step 3: save.
function openInspection(ordinal) {
  const p=state.project.panels.find(x=>x.ordinal===ordinal);if(!p)return;
  cleanupImage();state.selected=p;state.imageFile=null;state.croppedBlob=null;
  $('inspecting-id').textContent=`PANEL #${String(p.ordinal).padStart(2,'0')}`;
  $('inspecting-name').textContent=p.name;
  $('inspecting-location').textContent=`${p.floor} · ${p.location}`;
  $('temperature').value=p.measured?p.temperature:(Number.isFinite(Number(p.original_temperature))&&p.original_temperature.trim()!==''&&p.original_temperature!=='-'?p.original_temperature:'');
  $('inspection-date').value=p.date||todayLocal();
  $('keep-photo').classList.toggle('hidden',!p.measured);
  $('clear-reading').classList.toggle('hidden',!p.measured);
  $('capture-camera').value='';$('capture-file').value='';
  $('crop-viewport').style.aspectRatio=String(p.photo_ratio);
  setStep(1);openModal('inspection-modal');
}
function setStep(step){
  state.currentStep=step;
  for(let n=1;n<=3;n++){
    $(`stage-${n}`).classList.toggle('hidden',n!==step);
    const marker=document.querySelector(`.wizard-step[data-step="${n}"]`);
    marker.classList.toggle('active',n===step);marker.classList.toggle('passed',n<step);
  }
  if(step===2)requestAnimationFrame(()=>{state.panX=0;state.panY=0;state.zoom=1;$('zoom-slider').value='1';updateCrop();});
}
function cleanupImage(){
  if(state.objectUrl) URL.revokeObjectURL(state.objectUrl);
  if(state.previewUrl) URL.revokeObjectURL(state.previewUrl);
  state.objectUrl=null;state.previewUrl=null;state.image=null;state.drag=null;
}
async function handlePhoto(file){
  if(!file)return;
  if(file.size>12*1024*1024){toast('사진 크기는 12MB 이하만 지원합니다.',true);return;}
  if(!file.type.startsWith('image/')){toast('이미지 파일을 선택해 주세요.',true);return;}
  if(state.objectUrl)URL.revokeObjectURL(state.objectUrl);
  state.objectUrl=URL.createObjectURL(file);state.imageFile=file;
  const img=new Image();img.src=state.objectUrl;
  try{await img.decode();}catch{toast('사진을 불러오지 못했습니다.',true);return;}
  state.image=img;$('crop-image').src=state.objectUrl;setStep(2);
}
function cropGeometry(){
  const box=$('crop-viewport');
  if(!state.image||!box.clientWidth||!box.clientHeight)return null;
  const viewportW=box.clientWidth, viewportH=box.clientHeight;
  const naturalW=state.image.naturalWidth,naturalH=state.image.naturalHeight;
  const scale=Math.max(viewportW/naturalW,viewportH/naturalH)*state.zoom;
  const width=naturalW*scale, height=naturalH*scale;
  const limitX=Math.max(0,(width-viewportW)/2),limitY=Math.max(0,(height-viewportH)/2);
  state.panX=Math.max(-limitX,Math.min(limitX,state.panX));
  state.panY=Math.max(-limitY,Math.min(limitY,state.panY));
  return {viewportW,viewportH,width,height,left:(viewportW-width)/2+state.panX,top:(viewportH-height)/2+state.panY};
}
function updateCrop(){
  const geo=cropGeometry();if(!geo)return;
  const img=$('crop-image');Object.assign(img.style,{width:`${geo.width}px`,height:`${geo.height}px`,left:`${geo.left}px`,top:`${geo.top}px`});
  $('zoom-value').textContent=`${Math.round(state.zoom*100)}%`;
}
function cropPhoto(){
  const geo=cropGeometry();if(!geo)throw new Error('사진을 자를 수 없습니다.');
  const outputW=1200,outputH=Math.round(outputW/state.selected.photo_ratio);
  const canvas=document.createElement('canvas');canvas.width=outputW;canvas.height=outputH;
  const ctx=canvas.getContext('2d');ctx.imageSmoothingQuality='high';
  const ratio=outputW/geo.viewportW;
  ctx.drawImage(state.image,geo.left*ratio,geo.top*ratio,geo.width*ratio,geo.height*ratio);
  return new Promise(resolve=>canvas.toBlob(resolve,'image/jpeg',.92));
}
async function completeCrop(){
  const button=$('crop-next');button.disabled=true;
  try{
    const blob=await cropPhoto();if(!blob)throw new Error('사진 변환에 실패했습니다.');
    state.croppedBlob=blob;
    if(state.previewUrl)URL.revokeObjectURL(state.previewUrl);
    state.previewUrl=URL.createObjectURL(blob);
    $('cropped-preview').src=state.previewUrl;setStep(3);
  }catch(e){toast(e.message,true);}finally{button.disabled=false;}
}
async function keepPhoto(){
  if(!state.selected.measured)return;
  state.croppedBlob=null;
  $('cropped-preview').src=`/api/projects/${state.projectId}/panels/${state.selected.ordinal}/photo?v=${Date.now()}`;
  setStep(3);
}
async function saveInspection(e){
  e.preventDefault();if(!state.selected)return;
  const temp=$('temperature').value, date=$('inspection-date').value;
  if(temp===''||!date)return toast('온도와 점검일을 모두 입력해 주세요.',true);
  const button=$('save-reading');button.disabled=true;button.textContent='저장 중…';
  try {
    const fd=new FormData();fd.append('temperature',temp);fd.append('check_date',date);
    if(state.croppedBlob)fd.append('photo',state.croppedBlob,`panel_${state.selected.ordinal}.jpg`);
    await getJson(`/api/projects/${state.projectId}/panels/${state.selected.ordinal}`,{method:'POST',body:fd});
    closeModal('inspection-modal');cleanupImage();await loadCurrentProject();
    toast(`${state.selected.name} 측정 결과를 저장했습니다.`);
  }catch(e){toast(e.message,true);}finally{button.disabled=false;button.textContent='측정 결과 저장';}
}
async function clearInspection(){
  if(!state.selected||!confirm(`${state.selected.name} 측정 기록과 사진을 삭제할까요?`))return;
  try{
    await api(`/api/projects/${state.projectId}/panels/${state.selected.ordinal}`,{method:'DELETE'});
    closeModal('inspection-modal');cleanupImage();await loadCurrentProject();toast('측정 기록을 삭제했습니다.');
  }catch(e){toast(e.message,true);}
}
async function renameProject(){
  if(!state.project)return;
  const text=prompt('현장 이름을 입력해 주세요.',state.project.name);if(text===null||!text.trim())return;
  try{
    await getJson(`/api/projects/${state.projectId}`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:text.trim()})});
    await loadProjects(state.projectId);toast('현장 이름을 변경했습니다.');
  }catch(e){toast(e.message,true);}
}
async function removeProject(){
  if(!state.project||!confirm(`"${state.project.name}" 현장과 측정 사진·기록을 모두 삭제할까요? 복구할 수 없습니다.`))return;
  try{await api(`/api/projects/${state.projectId}`,{method:'DELETE'});state.projectId=null;await loadProjects();toast('현장을 삭제했습니다.');}
  catch(e){toast(e.message,true);}
}

$('login-form').addEventListener('submit',async e=>{
  e.preventDefault();try{
    await getJson('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:$('password').value})});
    $('password').value='';await boot();
  }catch(err){toast(err.message,true);}
});
$('logout').addEventListener('click',async()=>{await api('/api/auth/logout',{method:'POST'});$('app-layout').classList.add('hidden');$('auth-screen').classList.remove('hidden');});
for(const id of ['side-upload','upload-btn','empty-upload']) $(id).addEventListener('click',startUpload);
$('upload-form').addEventListener('submit',submitUpload);
$('upload-file').addEventListener('change',()=>{$('file-drop-title').textContent=$('upload-file').files[0]?.name||'.xlsx 파일을 선택하세요';});
$('download-btn').addEventListener('click',downloadProject);
$('project-select').addEventListener('change',e=>switchProject(e.target.value));
$('rename-project').addEventListener('click',renameProject);
$('remove-project').addEventListener('click',removeProject);
$('search').addEventListener('input',e=>{state.search=e.target.value;if(state.project)renderPanels();});
$('status-filter').addEventListener('change',e=>{state.status=e.target.value;if(state.project)renderPanels();});
for(const btn of document.querySelectorAll('[data-close]'))btn.addEventListener('click',()=>{closeModal(btn.dataset.close);if(btn.dataset.close==='inspection-modal')cleanupImage();});
for(const modal of document.querySelectorAll('.modal-backdrop'))modal.addEventListener('click',e=>{if(e.target===modal){closeModal(modal.id);if(modal.id==='inspection-modal')cleanupImage();}});
window.addEventListener('keydown',e=>{if(e.key==='Escape')for(const modal of document.querySelectorAll('.modal-backdrop:not(.hidden)')){closeModal(modal.id);if(modal.id==='inspection-modal')cleanupImage();}});
$('capture-camera').addEventListener('change',e=>handlePhoto(e.target.files[0]));
$('capture-file').addEventListener('change',e=>handlePhoto(e.target.files[0]));
$('zoom-slider').addEventListener('input',e=>{state.zoom=Number(e.target.value);updateCrop();});
$('crop-viewport').addEventListener('pointerdown',e=>{
  if(!state.image)return;e.preventDefault();$('crop-viewport').setPointerCapture(e.pointerId);
  state.drag={id:e.pointerId,origX:e.clientX,origY:e.clientY,panX:state.panX,panY:state.panY};
});
$('crop-viewport').addEventListener('pointermove',e=>{
  if(!state.drag||state.drag.id!==e.pointerId)return;
  state.panX=state.drag.panX+(e.clientX-state.drag.origX);
  state.panY=state.drag.panY+(e.clientY-state.drag.origY);updateCrop();
});
for(const eventName of ['pointerup','pointercancel','lostpointercapture'])$('crop-viewport').addEventListener(eventName,()=>{state.drag=null;});
window.addEventListener('resize',()=>{if(state.currentStep===2)updateCrop();});
$('crop-back').addEventListener('click',()=>setStep(1));
$('crop-next').addEventListener('click',completeCrop);
$('keep-photo').addEventListener('click',keepPhoto);
$('temperature-back').addEventListener('click',()=>setStep(state.image?2:1));
$('stage-3').addEventListener('submit',saveInspection);
$('clear-reading').addEventListener('click',clearInspection);
boot().catch(e=>toast(e.message,true));
