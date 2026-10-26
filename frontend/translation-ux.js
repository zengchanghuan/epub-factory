/* Translation presentation only. Never reprices, uploads or authorizes payment. */
(function(root){
  'use strict';
  const languages=['zh-CN','zh-TW','en','ja','ko'];
  const rows={
    tx_target:['请选择目标语言','請選擇目標語言','Choose a target language','翻訳先の言語を選択','대상 언어 선택'],
    tx_uploading:['正在上传…','正在上傳…','Uploading…','アップロード中…','업로드 중…'],
    tx_settings_error:['请检查术语与角色格式后重试。','請檢查術語與角色格式後重試。','Check glossary and character formatting, then retry.','用語集と登場人物の書式を確認してください。','용어집과 등장인물 형식을 확인 후 다시 시도하세요.'],
    tx_completed:['翻译完成，可以下载。','翻譯完成，可以下載。','Translation complete. Your download is ready.','翻訳完了。ダウンロードできます。','번역 완료. 다운로드할 수 있습니다.'],
    tx_payment:['等待付款，付款确认后自动翻译。','等待付款，確認後自動翻譯。','Awaiting payment. Translation starts after payment confirmation.','支払い待ち。入金確認後に翻訳を開始します。','결제 대기 중. 결제 확인 후 번역을 시작합니다.'],
    tx_same:['原文和目标语言相同，请重新选择','原文與目標語言相同，請重新選擇','Source and target must be different','原文と翻訳先は異なる言語を選択','원문과 대상 언어를 다르게 선택하세요'],
    tx_quote:['上传并查看翻译报价','上傳並查看翻譯報價','Upload and get a translation quote','アップロードして翻訳見積もりを確認','업로드 및 번역 견적 확인'],
    tx_advanced:['高级翻译设置（可选）','進階翻譯設定（選填）','Advanced translation settings (optional)','翻訳の詳細設定（任意）','고급 번역 설정 (선택)'],
    tx_direction:['翻译方向：{source} → {target}','翻譯方向：{source} → {target}','Translation: {source} → {target}','翻訳方向：{source} → {target}','번역 방향: {source} → {target}'],
    tx_auto:['自动识别','自動識別','Auto-detect','自動判定','자동 감지'],
    tx_analysis:['分析结果','分析結果','Analysis','分析結果','분석 결과'],
    tx_strategy:['翻译策略','翻譯策略','Translation style','翻訳方針','번역 방식'],
    tx_confidence:['分析置信度','分析置信度','Analysis confidence','分析の信頼度','분석 신뢰도'],
    tx_complexity:['语言复杂度','語言複雜度','Language complexity','言語の複雑さ','언어 복잡도'],
    tx_unknown:['未知','未知','Unknown','不明','알 수 없음'],
    tx_no_evidence:['暂无分析依据，采用默认策略。','暫無分析依據，採用預設策略。','No analysis evidence; using the default style.','分析情報がないため既定の方針を使用します。','분석 근거가 없어 기본 방식을 사용합니다.'],
    tx_sampling:['抽样：目录 {toc} 项，正文 {samples} 处，共 {chars} 字符。','抽樣：目錄 {toc} 項，正文 {samples} 處，共 {chars} 字元。','Sample: {toc} contents entries, {samples} passages, {chars} characters.','抽出：目次 {toc} 項目、本文 {samples} 箇所、{chars} 文字。','표본: 목차 {toc}개, 본문 {samples}곳, {chars}자.'],
    tx_original:['以下为原始分析或错误详情，可能保留原语言：','以下為原始分析或錯誤詳情，可能保留原語言：','Original analysis or error details may retain their source language:','以下の分析・エラー詳細は原言語の場合があります：','아래 분석 또는 오류 상세는 원래 언어로 표시될 수 있습니다:'],
    tx_chunks:['{count} 段','{count} 段','{count} passages','{count} 段落','{count}개 단락'],
    tx_inherit:['继承全书策略','繼承全書策略','Use book-wide style','全体の方針を使用','전체 책 방식 사용'],
    tx_chars:['约 {count}K 字符','約 {count}K 字元','About {count}K characters','約 {count}K 文字','약 {count}K자'],
    tx_price:['翻译服务价：{amount}','翻譯服務價：{amount}','Translation price: {amount}','翻訳料金：{amount}','번역 요금: {amount}'],
    tx_frozen:['以订单已确认金额为准；缓存复用以执行统计为准','以訂單已確認金額為準；快取重用以執行統計為準','The confirmed order amount applies; cache reuse is measured during processing.','確定した注文金額が適用されます。キャッシュ利用は処理時に集計します。','확정된 주문 금액이 적용되며 캐시 재사용은 처리 중 집계됩니다.'],
    tx_no_cache:['不复用旧译文缓存，本次不计缓存优惠','不重用舊譯文快取，本次不計快取優惠','No previous translation cache reuse or cache discount.','既存の翻訳キャッシュと割引は使用しません。','이전 번역 캐시와 캐시 할인은 사용하지 않습니다.'],
    tx_cache:['缓存复用以执行统计为准，本次报价未预扣缓存优惠','快取重用以執行統計為準，本次報價未預扣快取優惠','Cache reuse is measured during processing; no cache discount is deducted from this quote.','キャッシュ利用は処理時に集計します。この見積もりにキャッシュ割引はありません。','캐시 재사용은 처리 중 집계되며 이 견적에 캐시 할인은 반영되지 않습니다.'],
    tx_creating:['正在创建订单…','正在建立訂單…','Creating checkout…','決済を準備中…','결제 준비 중…'],
    tx_confirm_error:['确认失败，请检查详情后重试。','確認失敗，請檢查詳情後重試。','Confirmation failed. Review the details and retry.','確認できませんでした。詳細を確認して再試行してください。','확인하지 못했습니다. 상세 확인 후 다시 시도하세요.'],
    tx_upload_error:['上传未成功，请检查文件与网络后重试。','上傳未成功，請檢查檔案與網路後重試。','Upload failed. Check the file and connection, then retry.','アップロードできませんでした。ファイルと接続を確認してください。','업로드에 실패했습니다. 파일과 연결을 확인하세요.'],
    tx_role_error:['角色第 {line} 行格式错误','角色第 {line} 行格式錯誤','Invalid character entry on line {line}','登場人物の {line} 行目の形式が無効です','등장인물 {line}번째 줄 형식이 잘못되었습니다'],
    tx_single:['翻译一次处理一个 EPUB、TXT、DOCX 或 Markdown。请重新选择文件，或主动切换到基础转换。','翻譯一次處理一個 EPUB、TXT、DOCX 或 Markdown。請重新選檔，或主動切換至基本轉換。','Translation accepts one EPUB, TXT, DOCX or Markdown file. Choose a supported file or switch to basic conversion.','翻訳は EPUB・TXT・DOCX・Markdown 1冊ずつ対応します。ファイルを選び直すか、基本変換に切り替えてください。','번역은 EPUB, TXT, DOCX, Markdown 한 파일씩 지원합니다. 다시 선택하거나 기본 변환으로 전환하세요.'],
    tx_ready:['分析完成，请核对翻译方向、报价和设置。','分析完成，請核對翻譯方向、報價與設定。','Analysis complete. Check the language direction, quote and settings.','分析完了。翻訳方向・見積もり・設定を確認してください。','분석 완료. 번역 방향, 견적과 설정을 확인하세요.'],
    tx_processing:['翻译正在后台处理，完成后可下载。','翻譯正在背景處理，完成後可下載。','Translation is processing in the background. Download when complete.','バックグラウンドで翻訳中です。完了後にダウンロードできます。','백그라운드에서 번역 중입니다. 완료 후 다운로드하세요.'],
    tx_failed:['任务未完成。请保留订单编号并查看详情，勿重复付款。','任務未完成。請保留訂單編號並查看詳情，勿重複付款。','The task did not complete. Keep the order ID, review the details and do not pay again.','処理が完了していません。注文番号を保存し、詳細をご確認ください。再度支払わないでください。','작업이 완료되지 않았습니다. 주문 번호와 상세 내용을 확인하고 다시 결제하지 마세요.'],
    tx_details:['查看原始详情','查看原始詳情','View original details','元の詳細を見る','원본 상세 보기'],
    tx_layout:['双语阅读效果示意','雙語閱讀效果示意','Bilingual reading layout','対訳レイアウトの例','원문 대조 레이아웃'],
    tx_layout_note:['以下使用已公开的合成短文模型结果；目录与排版为示意，不代表整本书质量。','以下使用已公開的合成短文模型結果；目錄與排版為示意，不代表整本書品質。','Uses our published model results on synthetic passages. Contents and layout are illustrative, not whole-book quality evidence.','公開済みの合成短文モデル出力を使用。目次とレイアウトは模式例で、書籍全体の品質を保証しません。','공개된 합성 단문 모델 결과입니다. 목차와 레이아웃은 예시이며 책 전체 품질의 증거가 아닙니다.'],
    tx_chapter:['第 1 章 · 对话','第 1 章 · 對話','Chapter 1 · A conversation','第1章 · 会話','1장 · 대화'],
    tx_terms:['术语提示：Lin → 林；5 mg 和 10 mg 应保留数值关系。','術語提示：Lin → 林；5 mg 與 10 mg 應保留數值關係。','Terminology check: Lin; preserve the relationship between 5 mg and 10 mg.','用語確認：Lin。5 mg と 10 mg の数値関係を保持します。','용어 확인: Lin. 5 mg와 10 mg의 수치 관계를 유지합니다.']
  };
  const messages=Object.fromEntries(Object.entries(rows).map(([k,v])=>[k,Object.fromEntries(languages.map((l,i)=>[l,v[i]]))]));
  function text(key,values={},locale){
    const lang=locale||root.FixEpubI18n?.locale||'zh-CN';
    return (messages[key]?.[lang]||messages[key]?.['zh-CN']||key).replace(/\{(\w+)\}/g,(s,k)=>Object.hasOwn(values,k)?String(values[k]):s);
  }
  function languageName(code,locale){return code==='auto'?text('tx_auto',{},locale):({'zh-CN':'简体中文','zh-TW':'繁體中文',en:'English',ja:'日本語',ko:'한국어'}[code]||text('tx_target',{},locale));}
  function direction(source,target,locale){return text('tx_direction',{source:languageName(source||'auto',locale),target:languageName(target,locale)},locale);}
  function validDirection(source,target){return languages.includes(target)&&(source==='auto'||languages.includes(source))&&source!==target;}
  function entry(search,locale,tool){
    const query=new URLSearchParams(search),out=new URLSearchParams({lang:languages.includes(locale)?locale:'zh-CN'});
    if(tool==='translate'||query.get('tool')==='translate')out.set('tool','translate');
    for(const k of ['source','target']){const v=query.get(k);if(languages.includes(v)||k==='source'&&v==='auto')out.set(k,v);}
    return '/?'+out;
  }
  const api={messages,text,languageName,direction,validDirection,entry};
  if(typeof module==='object'&&module.exports)module.exports=api;
  else{Object.assign(root.FixEpubMessages,messages);root.FixEpubTranslationUX=api;}
})(typeof window!=='undefined'?window:globalThis);
