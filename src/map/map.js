'use strict';
const el=id=>document.getElementById(id);
let points=[],kind='',map,layer,nearest=false,origin=null,positionMarker;
const markers=new Map();
const status=text=>{el('status').textContent=text;};
function node(tag,text,cls){const n=document.createElement(tag);n.textContent=text;if(cls)n.className=cls;return n;}
function distance(p){const rad=x=>x*Math.PI/180,a=rad(origin[0]),b=rad(p.latitude),d=rad(p.longitude-origin[1]);return 6371000*2*Math.asin(Math.sqrt(Math.min(1,Math.sin((b-a)/2)**2+Math.cos(a)*Math.cos(b)*Math.sin(d/2)**2)));}
function card(p,popup=false){
 const box=node('article','','card');box.append(node('h2',p.name),node('p',p.address));
 if(nearest&&origin)box.append(node('p','Расстояние: '+(distance(p)/1000).toFixed(2)+' км'));
 if(kind==='fuel'){
  const brands=el('fuel').value?[el('fuel').value]:['АИ-92','АИ-95','ДТ'];
  for(const brand of brands){const f=p.fuels[brand];box.append(node('p',brand+': '+(!f?'нет сведений':f.available?'есть'+(f.price!=null?' · '+f.price+' ₽':''):'наличие не подтверждено')));if(f){box.append(node('p','Обновлено источником: '+f.updated_text+' МСК'));if(f.stale)box.append(node('p','Сведения старше часа; наличие могло измениться.','warning'));}}
  if(p.queue)box.append(node('p',p.queue.trim().toLowerCase()==='нет'?'Очереди нет.':'Очередь: '+p.queue));
  if(p.restrictions)box.append(node('p','Ограничения объёма в одни руки: '+p.restrictions));
 }
 if(!popup&&map){const b=node('button','На карте');b.type='button';b.onclick=()=>{map.setView([p.latitude,p.longitude],16);markers.get(p.id)?.openPopup();};box.append(b);}
 const a=node('a','Яндекс.Карты');a.href='https://yandex.ru/maps/?pt='+p.longitude+','+p.latitude+'&z=17&l=map';a.target='_blank';a.rel='noopener';box.append(a);return box;
}
function render(){
 const query=el('search').value.toLocaleLowerCase('ru').replaceAll('ё','е').trim(),brand=el('fuel').value;
 let shown=points.filter(p=>(p.name+' '+p.address).toLocaleLowerCase('ru').replaceAll('ё','е').includes(query)&&(!brand||p.fuels?.[brand]?.available));
 if(nearest&&origin)shown=shown.sort((a,b)=>distance(a)-distance(b)).slice(0,3);
 el('results').replaceChildren();if(layer)layer.clearLayers();markers.clear();
 for(const p of shown){el('results').append(card(p));if(map){const m=L.circleMarker([p.latitude,p.longitude],{radius:8,color:kind==='fuel'&&!Object.values(p.fuels).some(f=>f.available)?'#737e8a':'#1765b5',fillOpacity:.8}).bindPopup(card(p,true)).addTo(layer);markers.set(p.id,m);}}
 if(map&&shown.length){map.fitBounds(shown.map(p=>[p.latitude,p.longitude]),{padding:[24,24],maxZoom:15});}
 status(shown.length?'Показано '+shown.length+' из '+points.length+(nearest?' · три ближайшие по выбранным условиям':''):'По выбранным условиям точек нет.');
 if(!map)status(el('status').textContent+' Подложка карты недоступна; используйте список и ссылки.');
}
el('search').oninput=render;el('fuel').onchange=render;
el('all').onclick=()=>{nearest=false;el('search').value='';el('fuel').value='';render();};
el('nearby').onclick=()=>{
 if(!navigator.geolocation){status('Геолокация недоступна. Используйте поиск по адресу.');return;}
 el('nearby').disabled=true;status('Определяем местоположение…');
 navigator.geolocation.getCurrentPosition(pos=>{el('nearby').disabled=false;origin=[pos.coords.latitude,pos.coords.longitude];nearest=true;if(map){if(positionMarker)positionMarker.remove();positionMarker=L.circleMarker(origin,{radius:7,color:'#c7354b'}).bindPopup('Вы здесь').addTo(map);}render();},()=>{el('nearby').disabled=false;status('Не удалось получить геолокацию. Разрешите доступ в браузере или используйте поиск по адресу.');},{enableHighAccuracy:false,timeout:15000,maximumAge:60000});
};
(async()=>{try{
 const response=await fetch(location.pathname.replace(/\/$/,'')+'/data');if(!response.ok)throw Error('HTTP '+response.status);const data=await response.json();kind=data.kind;points=data.points;
 document.title=el('title').textContent=kind==='fuel'?'АЗС Костромской области':'Бесплатный Wi-Fi';el('fuelControl').hidden=kind!=='fuel';
 if(window.L){map=L.map('map').setView([57.77,40.93],8);layer=L.layerGroup().addTo(map);L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19,attribution:'&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'}).addTo(map).on('tileerror',()=>status('Подложка карты временно недоступна. Точки и список доступны.'));}
 el('nearby').disabled=false;el('all').disabled=false;render();
 }catch(error){status('Не удалось загрузить точки. Обновите страницу позже.');}})();
