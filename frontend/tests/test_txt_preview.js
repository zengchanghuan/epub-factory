const {analyze,controller,LIMIT}=require('../txt-preview.js');
const encode=s=>new TextEncoder().encode(s).buffer;
test('五语章标题、正文和编码预览与导入规则一致',()=>{
 for(const title of ['第一章 简体','第一章 繁體','Chapter 1: Start','第1章 日本語','제1장 시작']){
  const value=analyze(encode(`前言\r\n\r\n${title}\r\n<script>正文</script>`));assert.equal(value.count,1);assert.equal(value.headings[0].line,3);assert.equal(value.headings[0].title,title);assert(value.sample.includes('<script>正文</script>'));
 }
 const le=Buffer.from('\ufeffChapter 1\nBody','utf16le');assert.equal(analyze(le).encoding,'utf-16le');
 const be=Buffer.from(le);be.swap16();assert.equal(analyze(be).encoding,'utf-16be');
});
test('错误编码、控制字符、空文件和超限文件不显示成功预览',()=>{
 for(const data of [new Uint8Array([255,128]),encode('a\0b'),encode(' \n'),new Uint8Array(LIMIT+1)])assert.throws(()=>analyze(data));
});
test('目录和正文显示有界，长行不误识别为章节',()=>{
 const result=analyze(encode(Array.from({length:2100},(_,i)=>`Chapter ${i+1}\nText`).join('\n')));assert.equal(result.count,2000);assert.equal(result.headings.length,30);assert.equal([...result.sample].length,600);
 assert.equal(analyze(encode('Chapter 1 '+ 'a'.repeat(120))).count,0);
});
test('切换文件后迟到读取不会暴露前一文件正文',async()=>{
 let resolve;const views=[];const update=controller(x=>views.push(x));const waiting=update([{name:'a.txt',size:10,arrayBuffer:()=>new Promise(r=>resolve=r)}]);
 await update([{name:'b.epub',size:2}]);resolve(encode('private previous text'));await waiting;assert.equal(views.at(-1),null);
});
test('大文件预览不读取内容且不阻止原上传流程',async()=>{
 const views=[];await controller(x=>views.push(x))([{name:'big.txt',size:LIMIT+1,arrayBuffer:()=>{throw Error('must not read');}}]);assert.equal(views.at(-1).error,'large');
});
