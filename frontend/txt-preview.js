/* Local-only, bounded preview. Payment and server validation remain authoritative. */
(function(root){
  const LIMIT=2*1024*1024;
  const chapter=/^(?:(?:chapter|part|book)\s+(?:\d+|[ivxlcdm]+)\b|第\s*[0-9０-９一二三四五六七八九十百千万零〇兩两]+\s*[章節节部卷話话回]|제?\s*[0-9０-９]+\s*[장화부](?:\s|[.:：]|$))/i;
  function analyze(buffer){
    const data=new Uint8Array(buffer);
    if(data.length>LIMIT) throw new Error('large');
    const encoding=data[0]===255&&data[1]===254?'utf-16le':data[0]===254&&data[1]===255?'utf-16be':'utf-8';
    let text;try{text=new TextDecoder(encoding,{fatal:true}).decode(data);}catch(e){throw new Error('encoding');}
    if(!text.trim()) throw new Error('empty');
    if(/[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]/.test(text)) throw new Error('encoding');
    text=text.replace(/\r\n?/g,'\n');
    let count=0;const headings=[];
    for(const [index,line] of text.split('\n').entries()){
      const title=line.trim();
      if([...title].length<=120&&chapter.test(title)&&count<2000){count++;if(headings.length<30)headings.push({line:index+1,title});}
    }
    return {encoding,sample:[...text].slice(0,600).join(''),headings,count};
  }
  function controller(render){
    let revision=0;
    return async files=>{
      const current=++revision;
      if(files.length!==1||!files[0].name.toLowerCase().endsWith('.txt')){render(null);return;}
      render({loading:true});
      try{if(files[0].size>LIMIT)throw new Error('large');const value=analyze(await files[0].arrayBuffer());if(current===revision)render(value);}
      catch(e){if(current===revision)render({error:['large','empty','encoding'].includes(e.message)?e.message:'read'});}
    };
  }
  const api={analyze,controller,LIMIT};
  if(typeof module==='object'&&module.exports)module.exports=api;
  else{
    root.FixEpubTxtPreview=api;
    root.addEventListener('DOMContentLoaded',()=>{
      const panel=document.getElementById('txtPreview');
      const t=key=>root.FixEpubMessages[key]?.[document.documentElement.lang]||root.FixEpubMessages[key]?.['zh-CN']||key;
      let last;
      function render(value){
        last=value;panel.hidden=!value;if(!value)return;
        const status=document.getElementById('txtPreviewStatus'),sample=document.getElementById('txtPreviewSample'),list=document.getElementById('txtPreviewHeadings');
        sample.textContent='';list.replaceChildren();
        status.textContent=value.loading?t('txt_preview_loading'):value.error?t('txt_preview_'+value.error):`${value.encoding.toUpperCase()} · ${t('txt_preview_count')}: ${value.count}`;
        if(value.sample!==undefined){sample.textContent=value.sample;for(const h of value.headings){const li=document.createElement('li');li.textContent=`${h.line}: ${h.title}`;list.append(li);}if(!value.count){const li=document.createElement('li');li.textContent=t('txt_preview_none');list.append(li);}}
      }
      const update=controller(render);
      root.addEventListener('fixepub:files-selected',event=>update(event.detail));
      new MutationObserver(()=>render(last)).observe(document.documentElement,{attributes:true,attributeFilter:['lang']});
    });
  }
})(typeof window!=='undefined'?window:globalThis);
