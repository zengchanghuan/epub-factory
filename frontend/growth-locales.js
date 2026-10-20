/* Authored product and analytics labels; no user content. */
(function(root){const messages={
  "g_examples": {
    "zh-CN": "查看处理效果",
    "zh-TW": "查看處理效果",
    "en": "See sample results",
    "ja": "処理例を見る",
    "ko": "처리 예시 보기"
  },
  "g_title": {
    "zh-CN": "公开效果对照",
    "zh-TW": "公開效果對照",
    "en": "Before and after examples",
    "ja": "処理前後の比較",
    "ko": "처리 전후 비교"
  },
  "g_notice": {
    "zh-CN": "以下为合成短文的真实模型样本，不是客户文件，不代表整本书质量或人工审核通过。重要内容请结合原文复核。",
    "zh-TW": "以下為合成短文的真實模型樣本，不是客戶檔案，不代表整本書品質或人工審核通過。重要內容請對照原文。",
    "en": "These real model results use synthetic short passages, not customer files. They do not certify whole-book quality or human approval. Check important content against the source.",
    "ja": "合成した短文を実際のモデルで処理した例です。顧客のファイルではなく、書籍全体の品質や人による審査合格を保証しません。重要な内容は原文と照合してください。",
    "ko": "고객 파일이 아닌 합성된 짧은 글의 실제 모델 결과입니다. 책 전체의 품질이나 사람의 검수 통과를 보장하지 않습니다. 중요한 내용은 원문과 확인하세요."
  },
  "g_before": {
    "zh-CN": "处理前",
    "zh-TW": "處理前",
    "en": "Before",
    "ja": "処理前",
    "ko": "처리 전"
  },
  "g_after": {
    "zh-CN": "处理后",
    "zh-TW": "處理後",
    "en": "After",
    "ja": "処理後",
    "ko": "처리 후"
  },
  "g_translation": {
    "zh-CN": "翻译示例",
    "zh-TW": "翻譯示例",
    "en": "Translation example",
    "ja": "翻訳例",
    "ko": "번역 예시"
  },
  "g_proofreading": {
    "zh-CN": "精校示例",
    "zh-TW": "校對示例",
    "en": "Proofreading example",
    "ja": "校正例",
    "ko": "교정 예시"
  },
  "g_unchanged": {
    "zh-CN": "无需修改也是有效检查结果。",
    "zh-TW": "無需修改也是有效檢查結果。",
    "en": "No suggested edits is a valid review result.",
    "ja": "修正不要という結果もあります。",
    "ko": "수정할 내용이 없는 것도 정상적인 검토 결과입니다."
  },
  "g_upload": {
    "zh-CN": "上传并查看报价",
    "zh-TW": "上傳並查看報價",
    "en": "Upload and get a quote",
    "ja": "アップロードして見積もり",
    "ko": "업로드하고 견적 확인"
  },
  "g_quote": {
    "zh-CN": "付款前请确认",
    "zh-TW": "付款前請確認",
    "en": "Before you pay",
    "ja": "お支払い前に",
    "ko": "결제 전 확인"
  },
  "g_total": {
    "zh-CN": "以当前订单显示的总额和币种为准；PayPal 报价已含平台计入的支付手续费。",
    "zh-TW": "以目前訂單顯示的總額與幣別為準；PayPal 報價已含平台計入的支付手續費。",
    "en": "The current order shows your total and currency. PayPal quotes include the payment fees incorporated by FixEpub.",
    "ja": "現在の注文に合計金額と通貨を表示します。PayPal 見積もりには FixEpub が計上する決済手数料が含まれます。",
    "ko": "현재 주문에 표시된 총액과 통화를 확인하세요. PayPal 견적에는 FixEpub이 반영한 결제 수수료가 포함됩니다."
  },
  "g_deliver": {
    "zh-CN": "下载格式为 EPUB；翻译与精校另行报价，精校完成后可查看修改对照，不提供 DOCX 原格式导出。",
    "zh-TW": "下載格式為 EPUB；翻譯與校對另行報價，校對完成後可查看修改對照，不提供 DOCX 原格式匯出。",
    "en": "Downloads are EPUB. Translation and proofreading are separately quoted. Completed proofreading includes edit comparison; DOCX round-trip export is not provided.",
    "ja": "出力は EPUB です。翻訳・校正は別途見積もりとなります。校正後は変更を比較できます。DOCX 形式への再出力には対応していません。",
    "ko": "결과는 EPUB입니다. 번역과 교정은 별도 견적이며 교정 후 수정 내용을 비교할 수 있습니다. DOCX 원본 형식으로 내보내기는 지원하지 않습니다."
  },
  "g_wait": {
    "zh-CN": "耗时随篇幅、内容和排队情况变化，不承诺固定完成时间。付款后可关闭页面，使用同一浏览器回到任务中心查看和下载。",
    "zh-TW": "耗時依篇幅、內容與排隊狀況而變，不承諾固定完成時間。付款後可關閉頁面，使用同一瀏覽器回到任務中心查看及下載。",
    "en": "Time depends on length, content and queue; no fixed completion time is guaranteed. After payment, you can close the page and return using the same browser to view and download your task.",
    "ja": "所要時間は長さ・内容・待ち行列により変わります。完了時刻は保証しません。支払い後はページを閉じ、同じブラウザでタスク画面に戻って確認・ダウンロードできます。",
    "ko": "처리 시간은 분량, 내용, 대기열에 따라 달라집니다. 완료 시간을 보장하지 않습니다. 결제 후 페이지를 닫아도 같은 브라우저의 작업 화면에서 확인하고 다운로드할 수 있습니다."
  },
  "g_failure": {
    "zh-CN": "已付款但失败时，请保留订单号联系客服核实；不要重复付款。退款须符合退款政策，不是失败即自动退款。",
    "zh-TW": "已付款但失敗時，請保留訂單編號聯絡客服核實，勿重複付款。退款須符合退款政策，不是失敗即自動退款。",
    "en": "If a paid task fails, keep the order ID and contact support; do not pay again. Refunds follow the refund policy and are not automatically issued for every failure.",
    "ja": "支払い済みの処理が失敗した場合は注文番号を控えてサポートへ連絡し、再度支払わないでください。返金は規定に従い、失敗時に必ず自動返金されるわけではありません。",
    "ko": "결제한 작업이 실패하면 주문 번호를 보관하고 고객지원에 문의하세요. 다시 결제하지 마세요. 환불은 정책에 따르며 모든 실패에 자동 환불되지는 않습니다."
  },
  "g_refund": {
    "zh-CN": "退款政策",
    "zh-TW": "退款政策",
    "en": "Refund policy",
    "ja": "返金規定",
    "ko": "환불 정책"
  },
  "g_funnel": {
    "zh-CN": "付费漏斗",
    "zh-TW": "付費漏斗",
    "en": "Payment funnel",
    "ja": "決済ファネル",
    "ko": "결제 퍼널"
  },
  "g_scope": {
    "zh-CN": "范围：上线后有访问关联的转换、翻译、精校、修复、PDF 和批量订单。按首次访问日期分组，观察截至当前的结果；未关联历史订单不计入，不是收入报表。",
    "zh-TW": "範圍：上線後有造訪關聯的轉換、翻譯、校對、修復、PDF 與批次訂單。依首次造訪日期分組，觀察目前結果；未關聯歷史訂單不計入，不是收入報表。",
    "en": "Scope: attributed conversion, translation, proofreading, repair, PDF and batch orders since launch. Cohorts use first-visit date and outcomes so far. Unattributed history is excluded; this is not a revenue report.",
    "ja": "対象：公開後に訪問と関連付けられた変換・翻訳・校正・修復・PDF・一括注文。初回訪問日別の現時点の結果です。関連付けのない過去注文は含まず、売上報告ではありません。",
    "ko": "범위: 출시 후 방문과 연결된 변환·번역·교정·복구·PDF·일괄 주문. 첫 방문일 기준 현재까지의 결과입니다. 연결되지 않은 과거 주문은 제외되며 매출 보고서가 아닙니다."
  },
  "g_privacy": {
    "zh-CN": "访问数是浏览器会话数，不是独立人数；排除已标记测试订单、沙盒和常见机器人，不能保证过滤所有机器人。下载指已授权的文件响应，不能证明文件已保存。",
    "zh-TW": "造訪數是瀏覽器工作階段數，不是獨立人數；排除已標記測試訂單、沙盒與常見機器人，無法保證過濾全部機器人。下載指已授權的檔案回應，不代表檔案已儲存。",
    "en": "Visits count browser sessions, not unique people. Marked test orders, sandbox orders and common bots are excluded; bot filtering is not perfect. Downloads mean authorized file responses, not confirmed saves.",
    "ja": "訪問数はブラウザセッション数で人数ではありません。テスト注文・サンドボックス・一般的なボットを除外しますが、完全ではありません。ダウンロードは認可済みファイル応答で、保存完了を示しません。",
    "ko": "방문은 고유 인원이 아닌 브라우저 세션 수입니다. 표시된 테스트, 샌드박스, 일반 봇을 제외하지만 모든 봇을 거를 수는 없습니다. 다운로드는 인증된 파일 응답이며 저장 완료를 의미하지 않습니다."
  },
  "g_visits": {
    "zh-CN": "访问会话",
    "zh-TW": "造訪工作階段",
    "en": "Visit sessions",
    "ja": "訪問セッション",
    "ko": "방문 세션"
  },
  "g_selected": {
    "zh-CN": "选文件会话",
    "zh-TW": "選檔工作階段",
    "en": "File-selection sessions",
    "ja": "ファイル選択セッション",
    "ko": "파일 선택 세션"
  },
  "g_quoted": {
    "zh-CN": "已展示报价订单",
    "zh-TW": "已展示報價訂單",
    "en": "Quoted orders",
    "ja": "見積もり表示注文",
    "ko": "견적 표시 주문"
  },
  "g_clicked": {
    "zh-CN": "已点击付款订单",
    "zh-TW": "已點擊付款訂單",
    "en": "Payment-click orders",
    "ja": "支払いクリック注文",
    "ko": "결제 클릭 주문"
  },
  "g_paid": {
    "zh-CN": "已核验到账订单",
    "zh-TW": "已核驗到帳訂單",
    "en": "Verified paid orders",
    "ja": "入金検証済み注文",
    "ko": "입금 확인 주문"
  },
  "g_downloaded": {
    "zh-CN": "已响应下载订单",
    "zh-TW": "已回應下載訂單",
    "en": "Download-response orders",
    "ja": "ダウンロード応答注文",
    "ko": "다운로드 응답 주문"
  },
  "g_quote_to_paid": {
    "zh-CN": "报价 → 到账",
    "zh-TW": "報價 → 到帳",
    "en": "Quote → payment",
    "ja": "見積もり → 入金",
    "ko": "견적 → 입금"
  },
  "g_paid_to_download": {
    "zh-CN": "到账 → 下载响应",
    "zh-TW": "到帳 → 下載回應",
    "en": "Payment → download response",
    "ja": "入金 → ダウンロード応答",
    "ko": "입금 → 다운로드 응답"
  },
  "g_refresh": {
    "zh-CN": "刷新",
    "zh-TW": "重新整理",
    "en": "Refresh",
    "ja": "更新",
    "ko": "새로고침"
  },
  "g_login": {
    "zh-CN": "登录订单后台后再刷新",
    "zh-TW": "登入訂單後台後再重新整理",
    "en": "Log in to order admin, then refresh",
    "ja": "注文管理にログイン後、更新",
    "ko": "주문 관리에 로그인한 후 새로고침"
  },
  "g_error": {
    "zh-CN": "暂时无法读取，请稍后刷新。",
    "zh-TW": "暫時無法讀取，請稍後重新整理。",
    "en": "Unavailable. Try refreshing later.",
    "ja": "読み込めません。後で更新してください。",
    "ko": "읽을 수 없습니다. 나중에 새로고침하세요."
  },
  "g_days": {
    "zh-CN": "最近天数",
    "zh-TW": "最近天數",
    "en": "Days",
    "ja": "日数",
    "ko": "일수"
  },
  "g_language": {
    "zh-CN": "访问语言",
    "zh-TW": "造訪語言",
    "en": "Visit language",
    "ja": "訪問言語",
    "ko": "방문 언어"
  },
  "g_source": {
    "zh-CN": "来源",
    "zh-TW": "來源",
    "en": "Source",
    "ja": "参照元",
    "ko": "유입 경로"
  },
  "g_device": {
    "zh-CN": "设备",
    "zh-TW": "裝置",
    "en": "Device",
    "ja": "端末",
    "ko": "기기"
  },
  "g_all": {
    "zh-CN": "全部",
    "zh-TW": "全部",
    "en": "All",
    "ja": "すべて",
    "ko": "전체"
  },
  "g_direct": {
    "zh-CN": "直接访问",
    "zh-TW": "直接造訪",
    "en": "Direct",
    "ja": "直接",
    "ko": "직접"
  },
  "g_search": {
    "zh-CN": "搜索",
    "zh-TW": "搜尋",
    "en": "Search",
    "ja": "検索",
    "ko": "검색"
  },
  "g_community": {
    "zh-CN": "社区",
    "zh-TW": "社群",
    "en": "Community",
    "ja": "コミュニティ",
    "ko": "커뮤니티"
  },
  "g_social": {
    "zh-CN": "社交",
    "zh-TW": "社交",
    "en": "Social",
    "ja": "SNS",
    "ko": "소셜"
  },
  "g_campaign": {
    "zh-CN": "推广链接",
    "zh-TW": "推廣連結",
    "en": "Campaign",
    "ja": "キャンペーン",
    "ko": "캠페인"
  },
  "g_other": {
    "zh-CN": "其他",
    "zh-TW": "其他",
    "en": "Other",
    "ja": "その他",
    "ko": "기타"
  },
  "g_mobile": {
    "zh-CN": "移动设备",
    "zh-TW": "行動裝置",
    "en": "Mobile",
    "ja": "モバイル",
    "ko": "모바일"
  },
  "g_desktop": {
    "zh-CN": "电脑",
    "zh-TW": "電腦",
    "en": "Desktop",
    "ja": "パソコン",
    "ko": "데스크톱"
  },
  "g_conversion": {
    "zh-CN": "基础转换",
    "zh-TW": "基礎轉換",
    "en": "Conversion",
    "ja": "変換",
    "ko": "변환"
  },
  "g_service": {
    "zh-CN": "服务",
    "zh-TW": "服務",
    "en": "Service",
    "ja": "サービス",
    "ko": "서비스"
  },
  "g_empty": {
    "zh-CN": "尚无数据；不能视为 0% 转化率。",
    "zh-TW": "尚無資料；不能視為 0% 轉換率。",
    "en": "No data yet; this is not a 0% conversion rate.",
    "ja": "データなし。コンバージョン率 0% を意味しません。",
    "ko": "아직 데이터가 없습니다. 전환율 0%를 의미하지 않습니다."
  },
  "g_repair": {
    "zh-CN": "格式修复",
    "zh-TW": "格式修復",
    "en": "Format repair",
    "ja": "形式修復",
    "ko": "형식 복구"
  },
  "g_pdf": {
    "zh-CN": "PDF 原文转换",
    "zh-TW": "PDF 原文轉換",
    "en": "PDF conversion",
    "ja": "PDF 変換",
    "ko": "PDF 변환"
  },
  "g_batch": {
    "zh-CN": "批量订单",
    "zh-TW": "批次訂單",
    "en": "Batch orders",
    "ja": "一括注文",
    "ko": "일괄 주문"
  },
  "g_translation_service": {
    "zh-CN": "翻译",
    "zh-TW": "翻譯",
    "en": "Translation",
    "ja": "翻訳",
    "ko": "번역"
  },
  "g_proofreading_service": {
    "zh-CN": "精校",
    "zh-TW": "校對",
    "en": "Proofreading",
    "ja": "校正",
    "ko": "교정"
  }
}; if(typeof module==="object"&&module.exports)module.exports=messages;else Object.assign(root.FixEpubMessages,messages);})(typeof window!=="undefined"?window:globalThis);
