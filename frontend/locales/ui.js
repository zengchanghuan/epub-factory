/* Authored UI text only; never send book content to a translation service. */
(function(root){ const messages = {
  "ui_0": {
    "zh-CN": "✦ 转换 & 翻译",
    "zh-TW": "✦ 轉換 & 翻譯",
    "en": "✦ Convert & translate",
    "ja": "✦ 変換・翻訳",
    "ko": "✦ 변환 및 번역"
  },
  "ui_1": {
    "zh-CN": "🔧 格式修复",
    "zh-TW": "🔧 格式修復",
    "en": "🔧 EPUB repair",
    "ja": "🔧 EPUB 修復",
    "ko": "🔧 EPUB 복구"
  },
  "ui_2": {
    "zh-CN": "功能建议",
    "zh-TW": "功能建議",
    "en": "Suggest a feature",
    "ja": "機能を提案",
    "ko": "기능 제안"
  },
  "ui_3": {
    "zh-CN": "电子书翻译与精排版工具",
    "zh-TW": "電子書翻譯與精排版工具",
    "en": "Ebook translation & formatting",
    "ja": "電子書籍の翻訳・組版",
    "ko": "전자책 번역 및 서식 도구"
  },
  "ui_4": {
    "zh-CN": "上传转换",
    "zh-TW": "上傳轉換",
    "en": "Upload",
    "ja": "アップロード",
    "ko": "업로드"
  },
  "ui_5": {
    "zh-CN": "任务中心",
    "zh-TW": "任務中心",
    "en": "My tasks",
    "ja": "タスク一覧",
    "ko": "작업 목록"
  },
  "ui_6": {
    "zh-CN": "上传文件",
    "zh-TW": "上傳文件",
    "en": "Upload a file",
    "ja": "ファイルをアップロード",
    "ko": "파일 업로드"
  },
  "ui_7": {
    "zh-CN": "拖拽文件到这里，或点击选择",
    "zh-TW": "拖拽文件到這裏，或點擊選擇",
    "en": "Drop files here, or click to choose",
    "ja": "ここにファイルをドロップ、またはクリックして選択",
    "ko": "파일을 끌어 놓거나 클릭하여 선택"
  },
  "ui_8": {
    "zh-CN": "支持一次选择 1–10 个 .epub / .mobi / .azw3 / .docx / .md / .txt 文件；暂不支持 PDF",
    "zh-TW": "支持一次選擇 1–10 個 .epub / .mobi / .azw3 / .docx / .md / .txt 文件；暫不支持 PDF",
    "en": "Choose 1–10 EPUB, MOBI, AZW3, DOCX, Markdown or TXT files. Use the separate PDF tool for PDFs.",
    "ja": "EPUB・MOBI・AZW3・DOCX・Markdown・TXT を1〜10冊選択。PDF は専用ツールをご利用ください。",
    "ko": "EPUB, MOBI, AZW3, DOCX, Markdown, TXT 파일 1~10개를 선택하세요. PDF는 전용 도구를 이용하세요."
  },
  "ui_9": {
    "zh-CN": "选择整个文件夹",
    "zh-TW": "選擇整個文件夾",
    "en": "Choose a folder",
    "ja": "フォルダーを選択",
    "ko": "폴더 선택"
  },
  "ui_10": {
    "zh-CN": "转换 / 排版处理",
    "zh-TW": "轉換 / 排版處理",
    "en": "Convert / format",
    "ja": "変換・組版",
    "ko": "변환 및 서식"
  },
  "ui_11": {
    "zh-CN": "繁简转换、竖排改横排、阅读器适配，可选 用词精校。",
    "zh-TW": "繁簡轉換、豎排改橫排、閱讀器適配，可選 用詞精校。",
    "en": "Convert Chinese scripts, change vertical to horizontal layout, optimize for readers, and optionally review wording.",
    "ja": "繁簡変換、縦書きから横書き、端末向け最適化。用語校正は任意です。",
    "ko": "중국어 번체·간체 변환, 가로쓰기, 기기 최적화 및 선택적 용어 교정."
  },
  "ui_12": {
    "zh-CN": "电子书翻译",
    "zh-TW": "電子書翻譯",
    "en": "Translate an ebook",
    "ja": "電子書籍を翻訳",
    "ko": "전자책 번역"
  },
  "ui_13": {
    "zh-CN": "简体、繁体、英语、日语、韩语互译，可选双语对照和术语表。",
    "zh-TW": "簡體、繁體、英語、日語、韓語互譯，可選雙語對照和術語表。",
    "en": "Translate between English, Japanese, Korean, Simplified and Traditional Chinese, with optional bilingual output and glossary.",
    "ja": "英語・日本語・韓国語・簡体字中国語・繁体字中国語の相互翻訳。対訳と用語集も利用できます。",
    "ko": "영어·일본어·한국어·중국어 간체·번체 간 상호 번역. 대역과 용어집을 선택할 수 있습니다."
  },
  "ui_14": {
    "zh-CN": "转换设置",
    "zh-TW": "轉換設置",
    "en": "Conversion settings",
    "ja": "変換設定",
    "ko": "변환 설정"
  },
  "ui_15": {
    "zh-CN": "转换方向",
    "zh-TW": "轉換方向",
    "en": "Chinese script conversion",
    "ja": "中国語の変換方向",
    "ko": "중국어 문자 변환"
  },
  "ui_16": {
    "zh-CN": "台湾正体 → 横排简体（大陆）",
    "zh-TW": "臺灣正體 → 橫排簡體（大陸）",
    "en": "Taiwan Traditional → Mainland Simplified",
    "ja": "台湾繁体字 → 中国大陸簡体字（横書き）",
    "ko": "대만 번체 → 중국 본토 간체(가로쓰기)"
  },
  "ui_17": {
    "zh-CN": "港澳繁体 → 横排简体（大陆）",
    "zh-TW": "港澳繁體 → 橫排簡體（大陸）",
    "en": "Hong Kong Traditional → Mainland Simplified",
    "ja": "香港・マカオ繁体字 → 中国大陸簡体字（横書き）",
    "ko": "홍콩·마카오 번체 → 중국 본토 간체(가로쓰기)"
  },
  "ui_18": {
    "zh-CN": "繁体通用 → 横排简体",
    "zh-TW": "繁體通用 → 橫排簡體",
    "en": "Traditional → Simplified, horizontal",
    "ja": "繁体字 → 簡体字（横書き）",
    "ko": "번체 → 간체(가로쓰기)"
  },
  "ui_19": {
    "zh-CN": "横排简体 → 台湾正体",
    "zh-TW": "橫排簡體 → 臺灣正體",
    "en": "Simplified → Taiwan Traditional",
    "ja": "簡体字 → 台湾繁体字",
    "ko": "간체 → 대만 번체"
  },
  "ui_20": {
    "zh-CN": "横排简体 → 港澳繁体",
    "zh-TW": "橫排簡體 → 港澳繁體",
    "en": "Simplified → Hong Kong Traditional",
    "ja": "簡体字 → 香港・マカオ繁体字",
    "ko": "간체 → 홍콩·마카오 번체"
  },
  "ui_21": {
    "zh-CN": "横排简体 → 繁体通用",
    "zh-TW": "橫排簡體 → 繁體通用",
    "en": "Simplified → Traditional",
    "ja": "簡体字 → 繁体字",
    "ko": "간체 → 번체"
  },
  "ui_22": {
    "zh-CN": "阅读器适配",
    "zh-TW": "閱讀器適配",
    "en": "Reader profile",
    "ja": "読書端末",
    "ko": "읽기 기기"
  },
  "ui_23": {
    "zh-CN": "自动（推荐）",
    "zh-TW": "自動（推薦）",
    "en": "Automatic (recommended)",
    "ja": "自動（推奨）",
    "ko": "자동(권장)"
  },
  "ui_24": {
    "zh-CN": "Kindle 适合墨水屏；Apple Books 适合 iPhone/iPad 阅读。",
    "zh-TW": "Kindle 適合墨水屏；Apple Books 適合 iPhone/iPad 閱讀。",
    "en": "Choose Kindle for e-ink or Apple Books for iPhone/iPad.",
    "ja": "電子ペーパーには Kindle、iPhone/iPad には Apple Books を選択。",
    "ko": "전자잉크는 Kindle, iPhone/iPad는 Apple Books를 선택하세요."
  },
  "ui_25": {
    "zh-CN": "AI 用词精校（五种语言）",
    "zh-TW": "AI 用詞精校（五種語言）",
    "en": "AI proofreading (5 languages)",
    "ja": "AI 校正（5言語）",
    "ko": "AI 교정 (5개 언어)"
  },
  "ui_26": {
    "zh-CN": "支持简中、繁中、英、日、韩 EPUB / TXT。新增语言检查拼写、语法和用词，保留原语言；简中仍检查两岸用词。",
    "zh-TW": "支援簡中、繁中、英、日、韓 EPUB / TXT。新增語言檢查拼字、文法與用詞，保留原語言；簡中仍檢查兩岸用詞。",
    "en": "EPUB / TXT proofreading in Traditional Chinese, English, Japanese and Korean preserves the original language. Simplified Chinese reviews regional wording.",
    "ja": "繁体字・英語・日本語・韓国語の EPUB / TXT を同じ言語で校正します。簡体字は地域表現を確認します。",
    "ko": "번체 중국어·영어·일본어·한국어 EPUB / TXT을 원어로 교정합니다. 간체 중국어는 지역 용어를 검토합니다."
  },
  "ui_27": {
    "zh-CN": "用词精校为预付费服务，上传文件后自动估算字数与附加费用。系统会逐段检查上下文，任务耗时较长。",
    "zh-TW": "用詞精校爲預付費服務，上傳文件後自動估算字數與附加費用。系統會逐段檢查上下文，任務耗時較長。",
    "en": "Wording review is prepaid. Upload to estimate length and the add-on fee. Paragraph-by-paragraph review takes extra time.",
    "ja": "用語校正は前払いです。アップロード後に文字数と追加料金を見積もります。段落ごとの確認には時間がかかります。",
    "ko": "용어 교정은 선불 서비스입니다. 업로드 후 글자 수와 추가 요금을 계산합니다. 문단별 검토에는 시간이 더 걸립니다."
  },
  "ui_28": {
    "zh-CN": "预估字数：",
    "zh-TW": "預估字數：",
    "en": "Estimated characters:",
    "ja": "推定文字数：",
    "ko": "예상 글자 수:"
  },
  "ui_29": {
    "zh-CN": "字",
    "zh-TW": "字",
    "en": "characters",
    "ja": "文字",
    "ko": "자"
  },
  "ui_30": {
    "zh-CN": "精校附加费：",
    "zh-TW": "精校附加費：",
    "en": "Review add-on:",
    "ja": "校正の追加料金：",
    "ko": "교정 추가 요금:"
  },
  "ui_31": {
    "zh-CN": "电子书翻译设置",
    "zh-TW": "電子書翻譯設置",
    "en": "Translation settings",
    "ja": "翻訳設定",
    "ko": "번역 설정"
  },
  "ui_32": {
    "zh-CN": "标准翻译 ¥3.99 起，按字符阶梯计费，长书自动享受更低单价；高质量、文学模式及专业版按所选档位报价。",
    "zh-TW": "標準翻譯 ¥3.99 起，按字符階梯計費，長書自動享受更低單價；高質量、文學模式及專業版按所選檔位報價。",
    "en": "Standard translation starts at CNY 3.99, with volume tiers. Other quality levels and Pro are quoted separately. PayPal shows a USD quote.",
    "ja": "標準翻訳は CNY 3.99 から、文字数に応じた段階料金です。品質・Pro の選択に応じて見積もり、PayPal は米ドルで表示します。",
    "ko": "표준 번역은 CNY 3.99부터이며 글자 수에 따라 단계별 요금이 적용됩니다. 품질 및 Pro 선택에 따라 견적하며 PayPal은 USD로 표시합니다."
  },
  "ui_33": {
    "zh-CN": "费用在支付完成后才会扣除，本次任务不会二次扣费。",
    "zh-TW": "費用在支付完成後纔會扣除，本次任務不會二次扣費。",
    "en": "Confirm the quoted amount before paying. This task is not charged twice.",
    "ja": "見積金額を確認してお支払いください。同じタスクに二重請求は行いません。",
    "ko": "견적 금액을 확인한 후 결제하세요. 같은 작업에 중복 과금하지 않습니다."
  },
  "ui_34": {
    "zh-CN": "原文语言",
    "zh-TW": "原文語言",
    "en": "Source language",
    "ja": "原文の言語",
    "ko": "원문 언어"
  },
  "ui_35": {
    "zh-CN": "目标语言",
    "zh-TW": "目標語言",
    "en": "Target language",
    "ja": "翻訳先の言語",
    "ko": "목표 언어"
  },
  "ui_36": {
    "zh-CN": "自动识别",
    "zh-TW": "自動識別",
    "en": "Auto-detect",
    "ja": "自動判定",
    "ko": "자동 감지"
  },
  "ui_37": {
    "zh-CN": "简体中文",
    "zh-TW": "簡體中文",
    "en": "Simplified Chinese",
    "ja": "簡体字中国語",
    "ko": "중국어 간체"
  },
  "ui_38": {
    "zh-CN": "繁体中文",
    "zh-TW": "繁體中文",
    "en": "Traditional Chinese",
    "ja": "繁体字中国語",
    "ko": "중국어 번체"
  },
  "ui_39": {
    "zh-CN": "英语",
    "zh-TW": "英語",
    "en": "English",
    "ja": "英語",
    "ko": "영어"
  },
  "ui_40": {
    "zh-CN": "日语",
    "zh-TW": "日語",
    "en": "Japanese",
    "ja": "日本語",
    "ko": "일본어"
  },
  "ui_41": {
    "zh-CN": "韩语",
    "zh-TW": "韓語",
    "en": "Korean",
    "ja": "韓国語",
    "ko": "한국어"
  },
  "ui_42": {
    "zh-CN": "翻译策略",
    "zh-TW": "翻譯策略",
    "en": "Translation style",
    "ja": "翻訳方針",
    "ko": "번역 전략"
  },
  "ui_43": {
    "zh-CN": "（自动分析全书，也可人工锁定）",
    "zh-TW": "（自動分析全書，也可人工鎖定）",
    "en": "(Auto-analyzed, or choose manually)",
    "ja": "（自動分析、または手動で指定）",
    "ko": "(자동 분석 또는 직접 선택)"
  },
  "ui_44": {
    "zh-CN": "自动判断（推荐）",
    "zh-TW": "自動判斷（推薦）",
    "en": "Automatic (recommended)",
    "ja": "自動判定（推奨）",
    "ko": "자동 판단(권장)"
  },
  "ui_45": {
    "zh-CN": "中性忠实",
    "zh-TW": "中性忠實",
    "en": "Neutral and faithful",
    "ja": "中立・忠実",
    "ko": "중립적 충실 번역"
  },
  "ui_46": {
    "zh-CN": "文学叙事",
    "zh-TW": "文學敘事",
    "en": "Literary narrative",
    "ja": "文学的な叙述",
    "ko": "문학적 서술"
  },
  "ui_47": {
    "zh-CN": "学术严谨",
    "zh-TW": "學術嚴謹",
    "en": "Academic precision",
    "ja": "学術的な厳密さ",
    "ko": "학술적 엄밀성"
  },
  "ui_48": {
    "zh-CN": "镜像级忠实",
    "zh-TW": "鏡像級忠實",
    "en": "Strict fidelity",
    "ja": "原文に厳密に忠実",
    "ko": "원문에 엄격히 충실"
  },
  "ui_49": {
    "zh-CN": "实用技术",
    "zh-TW": "實用技術",
    "en": "Practical / technical",
    "ja": "実用・技術",
    "ko": "실용 및 기술"
  },
  "ui_50": {
    "zh-CN": "上传后先分析元数据、目录和正文样本；探针失败时自动回退，不阻塞翻译。",
    "zh-TW": "上傳後先分析元數據、目錄和正文樣本；探針失敗時自動回退，不阻塞翻譯。",
    "en": "Metadata, contents and text samples are analyzed after upload. If analysis fails, translation uses a fallback.",
    "ja": "アップロード後にメタデータ・目次・本文サンプルを分析。分析に失敗した場合は代替設定で翻訳します。",
    "ko": "업로드 후 메타데이터, 목차, 본문 표본을 분석합니다. 분석 실패 시 대체 설정으로 번역합니다."
  },
  "ui_51": {
    "zh-CN": "翻译质量",
    "zh-TW": "翻譯質量",
    "en": "Translation quality",
    "ja": "翻訳品質",
    "ko": "번역 품질"
  },
  "ui_52": {
    "zh-CN": "默认标准",
    "zh-TW": "默認標準",
    "en": "Standard by default",
    "ja": "標準が既定",
    "ko": "기본값: 표준"
  },
  "ui_53": {
    "zh-CN": "标准模式",
    "zh-TW": "標準模式",
    "en": "Standard",
    "ja": "標準",
    "ko": "표준"
  },
  "ui_54": {
    "zh-CN": "当前使用",
    "zh-TW": "當前使用",
    "en": "Selected",
    "ja": "選択中",
    "ko": "선택됨"
  },
  "ui_55": {
    "zh-CN": "自动初译、规则质检并复用兼容缓存，速度和成本优先。",
    "zh-TW": "自動初譯、規則質檢並複用兼容緩存，速度和成本優先。",
    "en": "Draft translation, rule-based checks and compatible cache reuse. Prioritizes speed and cost.",
    "ja": "初訳・ルール検査・互換キャッシュ再利用。速度とコストを優先。",
    "ko": "초벌 번역, 규칙 기반 검사, 호환 캐시 재사용. 속도와 비용을 우선합니다."
  },
  "ui_56": {
    "zh-CN": "高质量模式",
    "zh-TW": "高質量模式",
    "en": "High quality",
    "ja": "高品質",
    "ko": "고품질"
  },
  "ui_57": {
    "zh-CN": "按所选版本初译，结合章节上下文校对风险段落，复用已验证的同配置译文。",
    "zh-TW": "按所選版本初譯，結合章節上下文校對風險段落，複用已驗證的同配置譯文。",
    "en": "Draft with the selected model, review risky passages in chapter context, and reuse verified matching translations.",
    "ja": "選択モデルで初訳し、章の文脈で要注意段落を校正。同じ設定の検証済み訳文を再利用します。",
    "ko": "선택한 모델로 초벌 번역 후 장의 문맥으로 위험 문단을 검토하고 같은 설정의 검증된 번역을 재사용합니다."
  },
  "ui_58": {
    "zh-CN": "文学模式（信达雅）",
    "zh-TW": "文學模式（信達雅）",
    "en": "Literary",
    "ja": "文学",
    "ko": "문학"
  },
  "ui_59": {
    "zh-CN": "忠实初译后按章节润色，并回查原文语义；速度较慢，适合文学与人文书稿。",
    "zh-TW": "忠實初譯後按章節潤色，並回查原文語義；速度較慢，適合文學與人文書稿。",
    "en": "Faithful draft, chapter-level editing and semantic checks against the original. Slower; suited to literature and humanities.",
    "ja": "忠実な初訳後、章単位で編集し原文の意味を再確認。低速ですが文学・人文書に適しています。",
    "ko": "충실한 초벌 번역 후 장 단위로 다듬고 원문의 의미를 재검토합니다. 문학·인문서에 적합하며 더 오래 걸립니다."
  },
  "ui_60": {
    "zh-CN": "服务版本",
    "zh-TW": "服務版本",
    "en": "Model tier",
    "ja": "モデルの種類",
    "ko": "모델 등급"
  },
  "ui_61": {
    "zh-CN": "默认标准版",
    "zh-TW": "默認標準版",
    "en": "Standard by default",
    "ja": "標準が既定",
    "ko": "기본값: 표준"
  },
  "ui_62": {
    "zh-CN": "仅在手动选择时使用；选择 Flash 不会自动升级为 Pro。",
    "zh-TW": "僅在手動選擇時使用；選擇 Flash 不會自動升級爲 Pro。",
    "en": "Pro is used only when selected. Flash never upgrades automatically.",
    "ja": "Pro は手動選択時のみ使用。Flash から自動変更しません。",
    "ko": "Pro는 직접 선택할 때만 사용합니다. Flash가 자동으로 업그레이드되지는 않습니다."
  },
  "ui_63": {
    "zh-CN": "标准版",
    "zh-TW": "標準版",
    "en": "Standard",
    "ja": "標準版",
    "ko": "표준 등급"
  },
  "ui_64": {
    "zh-CN": "速度更快，适合结构简单、对成本更敏感的任务。",
    "zh-TW": "速度更快，適合結構簡單、對成本更敏感的任務。",
    "en": "Faster; suited to simpler books and cost-sensitive tasks.",
    "ja": "高速で、構造が単純な書籍や費用重視のタスクに適しています。",
    "ko": "구조가 단순한 책이나 비용에 민감한 작업에 적합하며 더 빠릅니다."
  },
  "ui_65": {
    "zh-CN": "表达灵活度",
    "zh-TW": "表達靈活度",
    "en": "Expression flexibility",
    "ja": "表現の柔軟性",
    "ko": "표현 유연성"
  },
  "ui_66": {
    "zh-CN": "（标准 0.3，高质量/文学 0.2；越低越稳定）",
    "zh-TW": "（標準 0.3，高質量/文學 0.2；越低越穩定）",
    "en": "(Standard 0.3; high quality/literary 0.2. Lower is more stable.)",
    "ja": "（標準0.3、高品質・文学0.2。低いほど安定。）",
    "ko": "(표준 0.3, 고품질·문학 0.2. 낮을수록 안정적입니다.)"
  },
  "ui_67": {
    "zh-CN": "双语并排",
    "zh-TW": "雙語並排",
    "en": "Bilingual output",
    "ja": "対訳表示",
    "ko": "대역 출력"
  },
  "ui_68": {
    "zh-CN": "原文 + 译文上下对照，便于校对",
    "zh-TW": "原文 + 譯文上下對照，便於校對",
    "en": "Original and translation together for comparison",
    "ja": "原文と訳文を上下に配置して比較",
    "ko": "원문과 번역을 위아래로 배치하여 비교"
  },
  "ui_69": {
    "zh-CN": "术语对照表",
    "zh-TW": "術語對照表",
    "en": "Glossary",
    "ja": "用語集",
    "ko": "용어집"
  },
  "ui_70": {
    "zh-CN": "（可选，每行一条）",
    "zh-TW": "（可選，每行一條）",
    "en": "(Optional, one entry per line)",
    "ja": "（任意、1行に1項目）",
    "ko": "(선택 사항, 한 줄에 한 항목)"
  },
  "ui_71": {
    "zh-CN": "AI 图书分析确认",
    "zh-TW": "AI 圖書分析確認",
    "en": "Review book analysis",
    "ja": "書籍分析の確認",
    "ko": "도서 분석 확인"
  },
  "ui_72": {
    "zh-CN": "支付前请确认文体、翻译策略、术语和角色。只有点击“确认并生成支付订单”后才会创建订单。",
    "zh-TW": "支付前請確認文體、翻譯策略、術語和角色。只有點擊“確認並生成支付訂單”後纔會創建訂單。",
    "en": "Review style, strategy, terminology and characters before paying. A checkout is issued only after confirmation.",
    "ja": "支払い前に文体・方針・用語・人物を確認してください。確認後に決済画面を発行します。",
    "ko": "결제 전에 문체, 전략, 용어, 인물을 확인하세요. 확인한 후에 결제 주문이 생성됩니다."
  },
  "ui_73": {
    "zh-CN": "查看判断依据与抽样范围",
    "zh-TW": "查看判斷依據與抽樣範圍",
    "en": "View analysis and sampling scope",
    "ja": "判断根拠とサンプル範囲を見る",
    "ko": "판단 근거와 표본 범위 보기"
  },
  "ui_74": {
    "zh-CN": "全书翻译策略",
    "zh-TW": "全書翻譯策略",
    "en": "Whole-book strategy",
    "ja": "全書の翻訳方針",
    "ko": "전체 도서 번역 전략"
  },
  "ui_75": {
    "zh-CN": "（可直接修订）",
    "zh-TW": "（可直接修訂）",
    "en": "(Editable)",
    "ja": "（編集可能）",
    "ko": "(수정 가능)"
  },
  "ui_76": {
    "zh-CN": "角色设定集",
    "zh-TW": "角色設定集",
    "en": "Character notes",
    "ja": "人物設定",
    "ko": "인물 설정"
  },
  "ui_77": {
    "zh-CN": "（每行：原名 = 译名 | 身份）",
    "zh-TW": "（每行：原名 = 譯名 | 身份）",
    "en": "(One per line: original = translation / role)",
    "ja": "（1行に1人：原名 = 訳名 / 役割）",
    "ko": "(한 줄에 한 명: 원명 = 번역명 / 역할)"
  },
  "ui_78": {
    "zh-CN": "章节级策略覆盖",
    "zh-TW": "章節級策略覆蓋",
    "en": "Chapter-specific overrides",
    "ja": "章ごとの方針指定",
    "ko": "장별 전략 재정의"
  },
  "ui_79": {
    "zh-CN": "人工调整原因",
    "zh-TW": "人工調整原因",
    "en": "Reason for changes",
    "ja": "変更理由",
    "ko": "수정 이유"
  },
  "ui_80": {
    "zh-CN": "（可选，便于复核）",
    "zh-TW": "（可選，便於複覈）",
    "en": "(Optional, for review)",
    "ja": "（任意、確認用）",
    "ko": "(선택 사항, 검토용)"
  },
  "ui_81": {
    "zh-CN": "生成双语对照版本",
    "zh-TW": "生成雙語對照版本",
    "en": "Create bilingual output",
    "ja": "対訳版を作成",
    "ko": "대역 버전 생성"
  },
  "ui_82": {
    "zh-CN": "在译文中标记已确认术语（默认关闭）",
    "zh-TW": "在譯文中標記已確認術語（默認關閉）",
    "en": "Highlight confirmed terms (off by default)",
    "ja": "確定用語を強調（既定はオフ）",
    "ko": "확정된 용어 강조(기본값: 끔)"
  },
  "ui_83": {
    "zh-CN": "确认并生成支付订单",
    "zh-TW": "確認並生成支付訂單",
    "en": "Confirm and proceed to payment",
    "ja": "確認して支払いへ",
    "ko": "확인 후 결제 진행"
  },
  "ui_84": {
    "zh-CN": "基础转换：",
    "zh-TW": "基礎轉換：",
    "en": "Base conversion:",
    "ja": "基本変換：",
    "ko": "기본 변환:"
  },
  "ui_85": {
    "zh-CN": "¥0.99 / 本",
    "zh-TW": "¥0.99 / 本",
    "en": "CNY 0.99 / book",
    "ja": "CNY 0.99 / 冊",
    "ko": "CNY 0.99 / 권"
  },
  "ui_86": {
    "zh-CN": "，支持繁简转换、竖排改横排、设备排版优化；用词精校另计。",
    "zh-TW": "，支持繁簡轉換、豎排改橫排、設備排版優化；用詞精校另計。",
    "en": ". Includes Chinese script conversion, horizontal layout and reader optimization. Wording review costs extra.",
    "ja": "。繁簡変換・横書き・端末最適化を含みます。用語校正は別料金。",
    "ko": ". 중국어 문자 변환, 가로쓰기, 기기 최적화 포함. 용어 교정은 별도 요금입니다."
  },
  "ui_87": {
    "zh-CN": "支付方式",
    "zh-TW": "支付方式",
    "en": "Payment method",
    "ja": "支払い方法",
    "ko": "결제 수단"
  },
  "ui_88": {
    "zh-CN": "支付宝 · 人民币",
    "zh-TW": "支付寶 · 人民幣",
    "en": "Alipay · CNY",
    "ja": "Alipay · 人民元",
    "ko": "Alipay · CNY"
  },
  "ui_89": {
    "zh-CN": "PayPal · 美元",
    "zh-TW": "PayPal · 美元",
    "en": "PayPal · USD",
    "ja": "PayPal · 米ドル",
    "ko": "PayPal · USD"
  },
  "ui_90": {
    "zh-CN": "PayPal：基础转换 US$0.99/本；AI 翻译、精校整单最低 US$1.49，报价已含支付手续费，付款前显示总价。",
    "zh-TW": "PayPal：基礎轉換 US$0.99/本；AI 翻譯、精校整單最低 US$1.49，報價已含支付手續費，付款前顯示總價。",
    "en": "PayPal: base conversion US$0.99/book. AI translation and review orders start at US$1.49, including payment processing fees. Review the total before payment.",
    "ja": "PayPal：基本変換は1冊 US$0.99。AI 翻訳・校正の注文は US$1.49 から、決済手数料込み。支払い前に総額を確認できます。",
    "ko": "PayPal: 기본 변환은 권당 US$0.99. AI 번역·교정 주문은 결제 수수료 포함 US$1.49부터이며 결제 전에 총액을 표시합니다."
  },
  "ui_91": {
    "zh-CN": "开始转换",
    "zh-TW": "開始轉換",
    "en": "Start conversion",
    "ja": "変換を開始",
    "ko": "변환 시작"
  },
  "ui_92": {
    "zh-CN": "扫码支付完成后自动开始转换",
    "zh-TW": "掃碼支付完成後自動開始轉換",
    "en": "Conversion starts after payment",
    "ja": "支払い後に変換を開始",
    "ko": "결제 후 자동 변환"
  },
  "ui_93": {
    "zh-CN": "基础转换 ¥0.99",
    "zh-TW": "基礎轉換 ¥0.99",
    "en": "Base conversion CNY 0.99",
    "ja": "基本変換 CNY 0.99",
    "ko": "기본 변환 CNY 0.99"
  },
  "ui_94": {
    "zh-CN": "使用支付宝 App 扫码 · 支付后自动处理",
    "zh-TW": "使用支付寶 App 掃碼 · 支付後自動處理",
    "en": "Scan with Alipay · processing starts after payment",
    "ja": "Alipay でスキャン・支払い後に処理",
    "ko": "Alipay로 스캔 · 결제 후 처리"
  },
  "ui_95": {
    "zh-CN": "应付金额",
    "zh-TW": "應付金額",
    "en": "Amount due",
    "ja": "お支払い金額",
    "ko": "결제 금액"
  },
  "ui_96": {
    "zh-CN": "前往支付宝付款",
    "zh-TW": "前往支付寶付款",
    "en": "Pay with Alipay",
    "ja": "Alipay で支払う",
    "ko": "Alipay로 결제"
  },
  "ui_97": {
    "zh-CN": "前往 PayPal 付款",
    "zh-TW": "前往 PayPal 付款",
    "en": "Pay with PayPal",
    "ja": "PayPal で支払う",
    "ko": "PayPal로 결제"
  },
  "ui_98": {
    "zh-CN": "所选支付页面将在小窗口中打开；付款确认后自动处理，可关闭本页稍后返回",
    "zh-TW": "所選支付頁面將在小窗口中打開；付款確認後自動處理，可關閉本頁稍後返回",
    "en": "Checkout opens in a new window. After payment is verified, processing continues in the background.",
    "ja": "決済画面は別ウィンドウで開きます。支払い確認後はバックグラウンドで処理します。",
    "ko": "결제 화면은 새 창에서 열립니다. 결제 확인 후 백그라운드에서 처리됩니다."
  },
  "ui_99": {
    "zh-CN": "PDF → EPUB · 保留原文",
    "zh-TW": "PDF → EPUB · 保留原文",
    "en": "PDF → EPUB · original language",
    "ja": "PDF → EPUB・原文を保持",
    "ko": "PDF → EPUB · 원문 유지"
  },
  "ui_100": {
    "zh-CN": "选择一个 PDF",
    "zh-TW": "選擇一個 PDF",
    "en": "Choose a PDF",
    "ja": "PDF を選択",
    "ko": "PDF 선택"
  },
  "ui_101": {
    "zh-CN": "PayPal · US$0.99 / 本",
    "zh-TW": "PayPal · US$0.99 / 本",
    "en": "PayPal · US$0.99 / book",
    "ja": "PayPal · US$0.99 / 冊",
    "ko": "PayPal · US$0.99 / 권"
  },
  "ui_102": {
    "zh-CN": "先检查并确认转换结果，再付款获取 EPUB。",
    "zh-TW": "先檢查並確認轉換結果，再付款獲取 EPUB。",
    "en": "Review the conversion first, then pay to download the EPUB.",
    "ja": "変換結果を確認してから支払い、EPUB をダウンロード。",
    "ko": "변환 결과를 먼저 확인한 후 결제하여 EPUB를 다운로드하세요."
  },
  "ui_103": {
    "zh-CN": "上传并检查 PDF",
    "zh-TW": "上傳並檢查 PDF",
    "en": "Upload and inspect PDF",
    "ja": "PDF をアップロード・検査",
    "ko": "PDF 업로드 및 검사"
  },
  "ui_104": {
    "zh-CN": "付款成功后关闭页面，任务仍会在后台执行。请用同一浏览器回来查看或下载，不要清理本站记录。",
    "zh-TW": "付款成功後關閉頁面，任務仍會在後臺執行。請用同一瀏覽器回來查看或下載，不要清理本站記錄。",
    "en": "After payment, work continues even if you close this page. Return in the same browser; keep this site's saved data.",
    "ja": "支払い後はページを閉じても処理が続きます。同じブラウザーで戻り、サイトデータを削除しないでください。",
    "ko": "결제 후 페이지를 닫아도 작업이 계속됩니다. 같은 브라우저로 돌아오고 사이트 데이터를 삭제하지 마세요."
  },
  "ui_105": {
    "zh-CN": "暂无任务，请先上传文件开始转换。",
    "zh-TW": "暫無任務，請先上傳文件開始轉換。",
    "en": "No tasks yet. Upload a file to begin.",
    "ja": "タスクはありません。ファイルをアップロードしてください。",
    "ko": "작업이 없습니다. 파일을 업로드하여 시작하세요."
  },
  "ui_106": {
    "zh-CN": "转换进度",
    "zh-TW": "轉換進度",
    "en": "Conversion progress",
    "ja": "変換の進捗",
    "ko": "변환 진행 상황"
  },
  "ui_107": {
    "zh-CN": "确认 PDF 转换方案",
    "zh-TW": "確認 PDF 轉換方案",
    "en": "Confirm PDF conversion plan",
    "ja": "PDF 変換プランを確認",
    "ko": "PDF 변환 계획 확인"
  },
  "ui_108": {
    "zh-CN": "我已了解上述处理范围，并接受逐项勾选的风险提示",
    "zh-TW": "我已瞭解上述處理範圍，並接受逐項勾選的風險提示",
    "en": "I understand the processing scope and accept the risks I selected.",
    "ja": "処理範囲を理解し、選択したリスクを承認します。",
    "ko": "처리 범위를 이해했으며 선택한 위험 사항에 동의합니다."
  },
  "ui_109": {
    "zh-CN": "继续支付",
    "zh-TW": "繼續支付",
    "en": "Continue payment",
    "ja": "支払いを続ける",
    "ko": "결제 계속"
  },
  "ui_110": {
    "zh-CN": "等待中",
    "zh-TW": "等待中",
    "en": "Waiting",
    "ja": "待機中",
    "ko": "대기 중"
  },
  "ui_111": {
    "zh-CN": "已耗时 00:00",
    "zh-TW": "已耗時 00:00",
    "en": "Elapsed 00:00",
    "ja": "経過 00:00",
    "ko": "경과 00:00"
  },
  "ui_112": {
    "zh-CN": "正在等待支付结果确认…",
    "zh-TW": "正在等待支付結果確認…",
    "en": "Waiting for payment verification…",
    "ja": "支払い確認を待っています…",
    "ko": "결제 확인 대기 중…"
  },
  "ui_113": {
    "zh-CN": "支付成功后会在后台自动处理，可以关闭本页，稍后回来查看",
    "zh-TW": "支付成功後會在後臺自動處理，可以關閉本頁，稍後回來查看",
    "en": "Processing continues in the background after payment. You can return later.",
    "ja": "支払い後はバックグラウンドで処理します。後で戻って確認できます。",
    "ko": "결제 후 백그라운드에서 처리됩니다. 나중에 돌아와 확인할 수 있습니다."
  },
  "ui_114": {
    "zh-CN": "付款成功后的任务会在后台处理。您可以关闭本页，稍后用同一浏览器在「任务中心」查看结果与下载。",
    "zh-TW": "付款成功後的任務會在後臺處理。您可以關閉本頁，稍後用同一瀏覽器在「任務中心」查看結果與下載。",
    "en": "Paid tasks run in the background. Return in the same browser to My tasks for results and downloads.",
    "ja": "支払い済みタスクはバックグラウンド処理。同じブラウザーのタスク一覧から結果を確認・取得できます。",
    "ko": "결제된 작업은 백그라운드에서 처리됩니다. 같은 브라우저의 작업 목록에서 결과를 확인하고 다운로드하세요."
  },
  "ui_115": {
    "zh-CN": "前往任务中心",
    "zh-TW": "前往任務中心",
    "en": "Go to My tasks",
    "ja": "タスク一覧へ",
    "ko": "작업 목록으로"
  },
  "ui_116": {
    "zh-CN": "在线预览 / 反馈",
    "zh-TW": "在線預覽 / 反饋",
    "en": "Preview / feedback",
    "ja": "プレビュー・フィードバック",
    "ko": "미리보기 및 피드백"
  },
  "ui_117": {
    "zh-CN": "↓ 下载处理结果",
    "zh-TW": "↓ 下載處理結果",
    "en": "↓ Download result",
    "ja": "↓ 結果をダウンロード",
    "ko": "↓ 결과 다운로드"
  },
  "ui_118": {
    "zh-CN": "停止翻译",
    "zh-TW": "停止翻譯",
    "en": "Stop translation",
    "ja": "翻訳を停止",
    "ko": "번역 중지"
  },
  "ui_119": {
    "zh-CN": "重启翻译",
    "zh-TW": "重啓翻譯",
    "en": "Restart translation",
    "ja": "翻訳を再開",
    "ko": "번역 다시 시작"
  },
  "ui_120": {
    "zh-CN": "查看详细信息（任务标识 / 日志 / 质量 / 耗时）",
    "zh-TW": "查看詳細信息（任務標識 / 日誌 / 質量 / 耗時）",
    "en": "Details (task ID / logs / quality / timing)",
    "ja": "詳細（タスクID・ログ・品質・時間）",
    "ko": "상세 정보(작업 ID / 로그 / 품질 / 시간)"
  },
  "ui_121": {
    "zh-CN": "文件名",
    "zh-TW": "文件名",
    "en": "Filename",
    "ja": "ファイル名",
    "ko": "파일 이름"
  },
  "ui_122": {
    "zh-CN": "输出模式",
    "zh-TW": "輸出模式",
    "en": "Output mode",
    "ja": "出力モード",
    "ko": "출력 모드"
  },
  "ui_123": {
    "zh-CN": "遇到问题？联系 QQ 一对一处理",
    "zh-TW": "遇到問題？聯繫 QQ 一對一處理",
    "en": "Need help? Contact support via QQ",
    "ja": "お困りですか？QQ でサポートに連絡",
    "ko": "도움이 필요하신가요? QQ로 지원 문의"
  },
  "ui_124": {
    "zh-CN": "打开 QQ 资料 · 添加客服",
    "zh-TW": "打開 QQ 資料 · 添加客服",
    "en": "Open QQ profile · add support",
    "ja": "QQ プロフィール・サポートを追加",
    "ko": "QQ 프로필 열기 · 지원 추가"
  },
  "ui_125": {
    "zh-CN": "复制 QQ 号",
    "zh-TW": "複製 QQ 號",
    "en": "Copy QQ ID",
    "ja": "QQ ID をコピー",
    "ko": "QQ ID 복사"
  },
  "ui_126": {
    "zh-CN": "也可邮件联系：",
    "zh-TW": "也可郵件聯繫：",
    "en": "Or email:",
    "ja": "メールでも連絡できます：",
    "ko": "이메일 문의:"
  },
  "ui_127": {
    "zh-CN": "翻译质检未通过",
    "zh-TW": "翻譯質檢未通過",
    "en": "Translation quality check failed",
    "ja": "翻訳品質検査に不合格",
    "ko": "번역 품질 검사 실패"
  },
  "ui_128": {
    "zh-CN": "免费重新翻译",
    "zh-TW": "免費重新翻譯",
    "en": "Retry translation for free",
    "ja": "無料で再翻訳",
    "ko": "무료 재번역"
  },
  "ui_129": {
    "zh-CN": "查看失败诊断",
    "zh-TW": "查看失敗診斷",
    "en": "View failure details",
    "ja": "失敗の詳細を見る",
    "ko": "실패 상세 보기"
  },
  "ui_130": {
    "zh-CN": "✨ 转换质量报告",
    "zh-TW": "✨ 轉換質量報告",
    "en": "✨ Conversion report",
    "ja": "✨ 変換レポート",
    "ko": "✨ 변환 보고서"
  },
  "ui_131": {
    "zh-CN": "清理冗余样式",
    "zh-TW": "清理冗餘樣式",
    "en": "Redundant styles removed",
    "ja": "不要スタイルを削除",
    "ko": "불필요한 스타일 제거"
  },
  "ui_132": {
    "zh-CN": "修复排版断层",
    "zh-TW": "修復排版斷層",
    "en": "Layout breaks repaired",
    "ja": "レイアウトを修復",
    "ko": "레이아웃 오류 복구"
  },
  "ui_133": {
    "zh-CN": "重建目录节点",
    "zh-TW": "重建目錄節點",
    "en": "Contents entries rebuilt",
    "ja": "目次項目を再構築",
    "ko": "목차 항목 재구성"
  },
  "ui_134": {
    "zh-CN": "防溢出保护对象",
    "zh-TW": "防溢出保護對象",
    "en": "Overflow protection",
    "ja": "はみ出し防止",
    "ko": "넘침 방지"
  },
  "ui_135": {
    "zh-CN": "🤖 翻译消耗：",
    "zh-TW": "🤖 翻譯消耗：",
    "en": "🤖 Translation usage:",
    "ja": "🤖 翻訳使用量：",
    "ko": "🤖 번역 사용량:"
  },
  "ui_136": {
    "zh-CN": "翻译耗时归因",
    "zh-TW": "翻譯耗時歸因",
    "en": "Translation timing",
    "ja": "翻訳時間の内訳",
    "ko": "번역 시간 분석"
  },
  "ui_137": {
    "zh-CN": "复制日志",
    "zh-TW": "複製日誌",
    "en": "Copy logs",
    "ja": "ログをコピー",
    "ko": "로그 복사"
  },
  "ui_138": {
    "zh-CN": "阶段耗时排行",
    "zh-TW": "階段耗時排行",
    "en": "Time by stage",
    "ja": "工程別の時間",
    "ko": "단계별 시간"
  },
  "ui_139": {
    "zh-CN": "失败类别",
    "zh-TW": "失敗類別",
    "en": "Failure category",
    "ja": "失敗の種類",
    "ko": "실패 유형"
  },
  "ui_140": {
    "zh-CN": "失败段落定位",
    "zh-TW": "失敗段落定位",
    "en": "Failed passages",
    "ja": "失敗した段落",
    "ko": "실패한 문단"
  },
  "ui_141": {
    "zh-CN": "建议动作",
    "zh-TW": "建議動作",
    "en": "Suggested action",
    "ja": "推奨される操作",
    "ko": "권장 조치"
  },
  "ui_142": {
    "zh-CN": "⏱ Pipeline 耗时",
    "zh-TW": "⏱ Pipeline 耗時",
    "en": "⏱ Processing time",
    "ja": "⏱ 処理時間",
    "ko": "⏱ 처리 시간"
  },
  "ui_143": {
    "zh-CN": "上一节",
    "zh-TW": "上一節",
    "en": "Previous chapter",
    "ja": "前の章",
    "ko": "이전 장"
  },
  "ui_144": {
    "zh-CN": "下一节",
    "zh-TW": "下一節",
    "en": "Next chapter",
    "ja": "次の章",
    "ko": "다음 장"
  },
  "ui_145": {
    "zh-CN": "关闭",
    "zh-TW": "關閉",
    "en": "Close",
    "ja": "閉じる",
    "ko": "닫기"
  },
  "ui_146": {
    "zh-CN": "正在读取章节…",
    "zh-TW": "正在讀取章節…",
    "en": "Loading chapter…",
    "ja": "章を読み込み中…",
    "ko": "장 불러오는 중…"
  },
  "ui_147": {
    "zh-CN": "反馈当前章节的问题：",
    "zh-TW": "反饋當前章節的問題：",
    "en": "Report an issue with this chapter:",
    "ja": "この章の問題を報告：",
    "ko": "현재 장의 문제 신고:"
  },
  "ui_148": {
    "zh-CN": "翻译",
    "zh-TW": "翻譯",
    "en": "Translation",
    "ja": "翻訳",
    "ko": "번역"
  },
  "ui_149": {
    "zh-CN": "目录",
    "zh-TW": "目錄",
    "en": "Contents",
    "ja": "目次",
    "ko": "목차"
  },
  "ui_150": {
    "zh-CN": "排版",
    "zh-TW": "排版",
    "en": "Layout",
    "ja": "組版",
    "ko": "서식"
  },
  "ui_151": {
    "zh-CN": "提交反馈",
    "zh-TW": "提交反饋",
    "en": "Send feedback",
    "ja": "フィードバックを送信",
    "ko": "피드백 보내기"
  },
  "ui_152": {
    "zh-CN": "💡 功能建议",
    "zh-TW": "💡 功能建議",
    "en": "💡 Feature request",
    "ja": "💡 機能リクエスト",
    "ko": "💡 기능 제안"
  },
  "ui_153": {
    "zh-CN": "您希望 FixEpub 增加什么功能？或者有什么改进建议？",
    "zh-TW": "您希望 FixEpub 增加什麼功能？或者有什麼改進建議？",
    "en": "What would you like us to add or improve?",
    "ja": "追加してほしい機能や改善案はありますか？",
    "ko": "추가하거나 개선했으면 하는 기능이 있나요?"
  },
  "ui_154": {
    "zh-CN": "提交建议",
    "zh-TW": "提交建議",
    "en": "Send suggestion",
    "ja": "提案を送信",
    "ko": "제안 보내기"
  },
  "ui_155": {
    "zh-CN": "关于我们",
    "zh-TW": "關於我們",
    "en": "About us",
    "ja": "運営情報",
    "ko": "소개"
  },
  "ui_156": {
    "zh-CN": "隐私政策",
    "zh-TW": "隱私政策",
    "en": "Privacy policy",
    "ja": "プライバシーポリシー",
    "ko": "개인정보 처리방침"
  },
  "ui_157": {
    "zh-CN": "服务条款",
    "zh-TW": "服務條款",
    "en": "Terms of service",
    "ja": "利用規約",
    "ko": "이용약관"
  },
  "ui_158": {
    "zh-CN": "退款政策",
    "zh-TW": "退款政策",
    "en": "Refund policy",
    "ja": "返金ポリシー",
    "ko": "환불 정책"
  },
  "ui_159": {
    "zh-CN": "联系我们",
    "zh-TW": "聯繫我們",
    "en": "Contact us",
    "ja": "お問い合わせ",
    "ko": "문의"
  },
  "ui_160": {
    "zh-CN": "公司业务简介与联系方式",
    "zh-TW": "公司業務簡介與聯繫方式",
    "en": "Company and contact information",
    "ja": "会社概要・連絡先",
    "ko": "회사 및 연락처"
  },
  "ui_161": {
    "zh-CN": "联系电话：",
    "zh-TW": "聯繫電話：",
    "en": "Phone:",
    "ja": "電話：",
    "ko": "전화:"
  },
  "ui_162": {
    "zh-CN": "EPUB 格式修复",
    "zh-TW": "EPUB 格式修復",
    "en": "EPUB repair",
    "ja": "EPUB 修復",
    "ko": "EPUB 복구"
  },
  "ui_163": {
    "zh-CN": "📂 上传文件",
    "zh-TW": "📂 上傳文件",
    "en": "📂 Upload a file",
    "ja": "📂 ファイルをアップロード",
    "ko": "📂 파일 업로드"
  },
  "ui_164": {
    "zh-CN": "点击选择或拖放 EPUB 文件",
    "zh-TW": "點擊選擇或拖放 EPUB 文件",
    "en": "Click or drop an EPUB file",
    "ja": "クリックまたは EPUB をドロップ",
    "ko": "클릭하거나 EPUB 파일을 끌어 놓으세요"
  },
  "ui_165": {
    "zh-CN": "仅支持 .epub，最大 60MB",
    "zh-TW": "僅支持 .epub，最大 60MB",
    "en": "EPUB only, up to 60 MB",
    "ja": "EPUB のみ、最大60 MB",
    "ko": "EPUB 전용, 최대 60MB"
  },
  "ui_166": {
    "zh-CN": "支付宝 · ¥0.99",
    "zh-TW": "支付寶 · ¥0.99",
    "en": "Alipay · CNY 0.99",
    "ja": "Alipay · CNY 0.99",
    "ko": "Alipay · CNY 0.99"
  },
  "ui_167": {
    "zh-CN": "请在上传前选择支付方式，诊断后锁定本次报价。",
    "zh-TW": "請在上傳前選擇支付方式，診斷後鎖定本次報價。",
    "en": "Select a payment method before uploading. The quote is locked after diagnosis.",
    "ja": "アップロード前に支払い方法を選択。診断後に見積金額が確定します。",
    "ko": "업로드 전에 결제 수단을 선택하세요. 진단 후 견적이 고정됩니다."
  },
  "ui_168": {
    "zh-CN": "文件格式正常",
    "zh-TW": "文件格式正常",
    "en": "No repair needed",
    "ja": "修復は不要です",
    "ko": "복구가 필요하지 않습니다"
  },
  "ui_169": {
    "zh-CN": "未发现任何兼容性问题，该文件无需修复。",
    "zh-TW": "未發現任何兼容性問題，該文件無需修復。",
    "en": "No compatibility issues found. This file needs no repair.",
    "ja": "互換性の問題は見つかりませんでした。修復は不要です。",
    "ko": "호환성 문제가 발견되지 않았습니다. 복구가 필요하지 않습니다."
  },
  "ui_170": {
    "zh-CN": "🔧 一键修复",
    "zh-TW": "🔧 一鍵修復",
    "en": "🔧 Repair EPUB",
    "ja": "🔧 EPUB を修復",
    "ko": "🔧 EPUB 복구"
  },
  "ui_171": {
    "zh-CN": "重新上传",
    "zh-TW": "重新上傳",
    "en": "Upload another file",
    "ja": "別のファイルをアップロード",
    "ko": "다른 파일 업로드"
  },
  "ui_172": {
    "zh-CN": "请完成付款",
    "zh-TW": "請完成付款",
    "en": "Complete payment",
    "ja": "お支払いください",
    "ko": "결제를 완료하세요"
  },
  "ui_173": {
    "zh-CN": "支付页面将在小窗口中打开；付款后可关闭本页，服务器会继续确认和修复。",
    "zh-TW": "支付頁面將在小窗口中打開；付款後可關閉本頁，服務器會繼續確認和修復。",
    "en": "Checkout opens in a new window. After paying, you can close this page; verification and repair continue on the server.",
    "ja": "決済画面は別ウィンドウで開きます。支払い後は閉じても、確認と修復が続きます。",
    "ko": "결제 화면은 새 창에서 열립니다. 결제 후 페이지를 닫아도 서버에서 확인과 복구가 계속됩니다."
  },
  "ui_174": {
    "zh-CN": "🛠 可修复的问题类型",
    "zh-TW": "🛠 可修復的問題類型",
    "en": "🛠 Supported repairs",
    "ja": "🛠 修復可能な問題",
    "ko": "🛠 복구 가능한 문제"
  },
  "ui_175": {
    "zh-CN": "错误码",
    "zh-TW": "錯誤碼",
    "en": "Error code",
    "ja": "エラーコード",
    "ko": "오류 코드"
  },
  "ui_176": {
    "zh-CN": "说明",
    "zh-TW": "說明",
    "en": "Description",
    "ja": "説明",
    "ko": "설명"
  },
  "ui_177": {
    "zh-CN": "修复支持",
    "zh-TW": "修復支持",
    "en": "Support",
    "ja": "対応",
    "ko": "지원"
  },
  "ui_178": {
    "zh-CN": "✓ 支持",
    "zh-TW": "✓ 支持",
    "en": "✓ Supported",
    "ja": "✓ 対応",
    "ko": "✓ 지원"
  },
  "ui_179": {
    "zh-CN": "其他",
    "zh-TW": "其他",
    "en": "Other",
    "ja": "その他",
    "ko": "기타"
  },
  "ui_180": {
    "zh-CN": "暂不支持",
    "zh-TW": "暫不支持",
    "en": "Not supported",
    "ja": "未対応",
    "ko": "지원하지 않음"
  },
  "ui_181": {
    "zh-CN": "确认订单并付款",
    "zh-TW": "確認訂單並付款",
    "en": "Review order and pay",
    "ja": "注文を確認して支払う",
    "ko": "주문 확인 및 결제"
  },
  "ui_182": {
    "zh-CN": "正在读取原订单…",
    "zh-TW": "正在讀取原訂單…",
    "en": "Loading original order…",
    "ja": "元の注文を読み込み中…",
    "ko": "원래 주문 불러오는 중…"
  },
  "ui_183": {
    "zh-CN": "以美元结算。付款后自动处理，可回到任务中心查看进度和下载文件。",
    "zh-TW": "以美元結算。付款後自動處理，可回到任務中心查看進度和下載文件。",
    "en": "Charged in USD. Processing starts after payment; return to My tasks for progress and downloads.",
    "ja": "米ドル決済です。支払い後に処理を開始。タスク一覧で進捗確認・ダウンロードできます。",
    "ko": "USD로 결제합니다. 결제 후 처리가 시작되며 작업 목록에서 진행 상황과 다운로드를 확인할 수 있습니다."
  },
  "ui_184": {
    "zh-CN": "我已付款，重新核验",
    "zh-TW": "我已付款，重新覈驗",
    "en": "I've paid — verify again",
    "ja": "支払い済み・再確認",
    "ko": "결제 완료 — 다시 확인"
  },
  "ui_185": {
    "zh-CN": "返回 FixEpub",
    "zh-TW": "返回 FixEpub",
    "en": "Return to FixEpub",
    "ja": "FixEpub に戻る",
    "ko": "FixEpub로 돌아가기"
  },
  "ui_186": {
    "zh-CN": "金额以本订单锁定的美元报价为准。",
    "zh-TW": "金額以本訂單鎖定的美元報價爲準。",
    "en": "The USD amount locked for this order applies.",
    "ja": "この注文に確定した米ドル金額が適用されます。",
    "ko": "이 주문에 고정된 USD 금액이 적용됩니다."
  },
  "ui_187": {
    "zh-CN": "不必一直等在这里",
    "zh-TW": "不必一直等在這裏",
    "en": "You can come back later",
    "ja": "後から戻って確認できます",
    "ko": "나중에 다시 확인하세요"
  },
  "ui_188": {
    "zh-CN": "完成后邮件提醒（选填）",
    "zh-TW": "完成後郵件提醒（選填）",
    "en": "Email me when ready (optional)",
    "ja": "完了時にメール通知（任意）",
    "ko": "완료 이메일 알림 (선택)"
  },
  "ui_189": {
    "zh-CN": "将用于下一次上传创建的任务。",
    "zh-TW": "將用於下一次上傳創建的任務。",
    "en": "Applies to your next upload.",
    "ja": "次回アップロードするタスクに適用します。",
    "ko": "다음 업로드 작업에 적용됩니다."
  },
  "ui_190": {
    "zh-CN": "保存邮箱",
    "zh-TW": "保存郵箱",
    "en": "Save email",
    "ja": "メールを保存",
    "ko": "이메일 저장"
  },
  "ui_191": {
    "zh-CN": "本机最近任务",
    "zh-TW": "本機最近任務",
    "en": "Recent tasks on this device",
    "ja": "この端末の最近のタスク",
    "ko": "이 기기의 최근 작업"
  },
  "ui_192": {
    "zh-CN": "✦ 转换",
    "zh-TW": "✦ 轉換",
    "en": "✦ Convert",
    "ja": "✦ 変換",
    "ko": "✦ 변환"
  },
  "ui_193": {
    "zh-CN": "📄 PDF 转 EPUB",
    "zh-TW": "📄 PDF 轉 EPUB",
    "en": "📄 PDF to EPUB",
    "ja": "📄 PDF を EPUB に",
    "ko": "📄 PDF를 EPUB로"
  },
  "ui_194": {
    "zh-CN": "上传无法在 Apple Books、Kindle 中打开的 EPUB 文件，自动检测格式问题并一键修复。处理结果可稍后回到本页下载，建议完成后及时保存到本机，请勿清理浏览器中的本站记录。",
    "zh-TW": "上傳無法在 Apple Books、Kindle 中打開的 EPUB 文件，自動檢測格式問題並一鍵修復。處理結果可稍後回到本頁下載，建議完成後及時保存到本機，請勿清理瀏覽器中的本站記錄。",
    "en": "Upload an EPUB that won't open in Apple Books or Kindle. We diagnose and repair supported format issues. Return here to download, save the result locally and keep this site's browser data.",
    "ja": "Apple Books や Kindle で開けない EPUB をアップロード。対応する形式問題を診断・修復します。後でこのページから取得し、端末に保存してください。サイトデータは削除しないでください。",
    "ko": "Apple Books 또는 Kindle에서 열리지 않는 EPUB를 업로드하세요. 지원되는 형식 문제를 진단·복구합니다. 나중에 돌아와 결과를 기기에 저장하고 사이트 데이터를 유지하세요."
  },
  "ui_195": {
    "zh-CN": "✓ mimetype 位置修复",
    "zh-TW": "✓ mimetype 位置修復",
    "en": "✓ mimetype ordering",
    "ja": "✓ mimetype の配置修正",
    "ko": "✓ mimetype 위치 복구"
  },
  "ui_196": {
    "zh-CN": "✓ DOCTYPE 标准化",
    "zh-TW": "✓ DOCTYPE 標準化",
    "en": "✓ DOCTYPE normalization",
    "ja": "✓ DOCTYPE の標準化",
    "ko": "✓ DOCTYPE 표준화"
  },
  "ui_197": {
    "zh-CN": "✓ OPF 命名空间修复",
    "zh-TW": "✓ OPF 命名空間修復",
    "en": "✓ OPF namespace repair",
    "ja": "✓ OPF 名前空間の修復",
    "ko": "✓ OPF 네임스페이스 복구"
  },
  "ui_198": {
    "zh-CN": "✓ Apple Books 兼容",
    "zh-TW": "✓ Apple Books 兼容",
    "en": "✓ Apple Books compatibility",
    "ja": "✓ Apple Books 互換性",
    "ko": "✓ Apple Books 호환성"
  },
  "ui_199": {
    "zh-CN": "/ 次",
    "zh-TW": "/ 次",
    "en": "/ repair",
    "ja": "/ 回",
    "ko": "/ 회"
  },
  "ui_200": {
    "zh-CN": "使用支付宝 App 扫码 · 支付完成后自动处理",
    "zh-TW": "使用支付寶 App 掃碼 · 支付完成後自動處理",
    "en": "Scan with Alipay · processing after payment",
    "ja": "Alipay でスキャン・支払い後に処理",
    "ko": "Alipay로 스캔 · 결제 후 처리"
  },
  "ui_201": {
    "zh-CN": "支付成功后无需任何操作，系统自动修复",
    "zh-TW": "支付成功後無需任何操作，系統自動修復",
    "en": "Repair starts automatically after verified payment",
    "ja": "支払い確認後、自動で修復します",
    "ko": "결제 확인 후 자동으로 복구합니다"
  },
  "ui_202": {
    "zh-CN": "通常 5 秒内完成，完成后可直接下载",
    "zh-TW": "通常 5 秒內完成，完成後可直接下載",
    "en": "Download when repair completes",
    "ja": "修復完了後にダウンロードできます",
    "ko": "복구 완료 후 다운로드할 수 있습니다"
  },
  "ui_203": {
    "zh-CN": "可以关闭页面，稍后用同一浏览器回来查看和下载，无需重新上传",
    "zh-TW": "可以關閉頁面，稍後用同一瀏覽器回來查看和下載，無需重新上傳",
    "en": "Close this page and return in the same browser later. No need to upload again.",
    "ja": "ページを閉じて後で同じブラウザーに戻れます。再アップロードは不要です。",
    "ko": "페이지를 닫고 나중에 같은 브라우저로 돌아오세요. 다시 업로드할 필요가 없습니다."
  },
  "ui_204": {
    "zh-CN": "我已付款，确认状态",
    "zh-TW": "我已付款，確認狀態",
    "en": "I've paid — check status",
    "ja": "支払い済み・状態を確認",
    "ko": "결제 완료 — 상태 확인"
  },
  "ui_205": {
    "zh-CN": "修复完成！所有可修复的格式问题已处理。",
    "zh-TW": "修復完成！所有可修復的格式問題已處理。",
    "en": "Repair complete. All repairable format issues have been processed.",
    "ja": "修復完了。修復可能な形式問題を処理しました。",
    "ko": "복구가 완료되었습니다. 복구 가능한 형식 문제를 모두 처리했습니다."
  },
  "ui_206": {
    "zh-CN": "⬇︎ 下载修复后的 EPUB",
    "zh-TW": "⬇︎ 下載修復後的 EPUB",
    "en": "⬇︎ Download repaired EPUB",
    "ja": "⬇︎ 修復済み EPUB を取得",
    "ko": "⬇︎ 복구된 EPUB 다운로드"
  },
  "ui_207": {
    "zh-CN": "修复另一个文件",
    "zh-TW": "修復另一個文件",
    "en": "Repair another file",
    "ja": "別のファイルを修復",
    "ko": "다른 파일 복구"
  },
  "ui_208": {
    "zh-CN": "可修复的问题类型",
    "zh-TW": "可修復的問題類型",
    "en": "Supported repairs",
    "ja": "修復可能な問題",
    "ko": "복구 가능한 문제"
  },
  "ui_209": {
    "zh-CN": "mimetype 文件缺失或不是 ZIP 第一个条目（导致阅读器无法识别为 EPUB）",
    "zh-TW": "mimetype 文件缺失或不是 ZIP 第一個條目（導致閱讀器無法識別爲 EPUB）",
    "en": "mimetype missing or not the first ZIP entry, preventing EPUB recognition",
    "ja": "mimetype がない、または ZIP の先頭でなく EPUB と認識できない問題",
    "ko": "mimetype가 없거나 ZIP의 첫 항목이 아니어서 EPUB로 인식되지 않는 문제"
  },
  "ui_210": {
    "zh-CN": "XHTML 文件使用旧版 DOCTYPE（XHTML 1.0/1.1），Apple Books / EPUB 3 不接受",
    "zh-TW": "XHTML 文件使用舊版 DOCTYPE（XHTML 1.0/1.1），Apple Books / EPUB 3 不接受",
    "en": "Legacy XHTML 1.0/1.1 DOCTYPE incompatible with Apple Books / EPUB 3",
    "ja": "Apple Books / EPUB 3 と互換性のない旧 XHTML 1.0/1.1 DOCTYPE",
    "ko": "Apple Books / EPUB 3과 호환되지 않는 이전 XHTML 1.0/1.1 DOCTYPE"
  },
  "ui_211": {
    "zh-CN": "OPF metadata 含非法",
    "zh-TW": "OPF metadata 含非法",
    "en": "Invalid OPF metadata attribute",
    "ja": "OPF メタデータの不正な属性",
    "ko": "OPF 메타데이터의 잘못된 속성"
  },
  "ui_212": {
    "zh-CN": "属性，导致元数据解析失败",
    "zh-TW": "屬性，導致元數據解析失敗",
    "en": "causing metadata parsing failure",
    "ja": "によるメタデータ解析エラー",
    "ko": "때문에 메타데이터 구문 분석 실패"
  },
  "ui_213": {
    "zh-CN": "内容损坏、加密保护、DRM 等结构性问题",
    "zh-TW": "內容損壞、加密保護、DRM 等結構性問題",
    "en": "Corrupted content, encryption or DRM",
    "ja": "内容の破損、暗号化、DRM",
    "ko": "콘텐츠 손상, 암호화 또는 DRM"
  },
  "ui_214": {
    "zh-CN": "登录",
    "zh-TW": "登錄",
    "en": "Sign in",
    "ja": "ログイン",
    "ko": "로그인"
  },
  "ui_215": {
    "zh-CN": "注册",
    "zh-TW": "註冊",
    "en": "Sign up",
    "ja": "新規登録",
    "ko": "회원가입"
  },
  "ui_216": {
    "zh-CN": "退出登录",
    "zh-TW": "退出登錄",
    "en": "Sign out",
    "ja": "ログアウト",
    "ko": "로그아웃"
  },
  "ui_217": {
    "zh-CN": "邮箱",
    "zh-TW": "郵箱",
    "en": "Email",
    "ja": "メール",
    "ko": "이메일"
  },
  "ui_218": {
    "zh-CN": "密码",
    "zh-TW": "密碼",
    "en": "Password",
    "ja": "パスワード",
    "ko": "비밀번호"
  },
  "ui_219": {
    "zh-CN": "手机号",
    "zh-TW": "手機號",
    "en": "Phone number",
    "ja": "電話番号",
    "ko": "전화번호"
  },
  "ui_220": {
    "zh-CN": "验证码",
    "zh-TW": "驗證碼",
    "en": "Verification code",
    "ja": "認証コード",
    "ko": "인증 코드"
  },
  "ui_221": {
    "zh-CN": "发送验证码",
    "zh-TW": "發送驗證碼",
    "en": "Send code",
    "ja": "コードを送信",
    "ko": "코드 보내기"
  },
  "ui_222": {
    "zh-CN": "取消",
    "zh-TW": "取消",
    "en": "Cancel",
    "ja": "キャンセル",
    "ko": "취소"
  },
  "ui_223": {
    "zh-CN": "确定",
    "zh-TW": "確定",
    "en": "Confirm",
    "ja": "確認",
    "ko": "확인"
  },
  "ui_224": {
    "zh-CN": "保存",
    "zh-TW": "保存",
    "en": "Save",
    "ja": "保存",
    "ko": "저장"
  },
  "ui_225": {
    "zh-CN": "重新获取支付入口",
    "zh-TW": "重新獲取支付入口",
    "en": "Reopen checkout",
    "ja": "支払い画面を再取得",
    "ko": "결제 화면 다시 열기"
  },
  "ui_226": {
    "zh-CN": "正在生成支付入口…",
    "zh-TW": "正在生成支付入口…",
    "en": "Preparing checkout…",
    "ja": "支払い画面を準備中…",
    "ko": "결제 화면 준비 중…"
  },
  "ui_227": {
    "zh-CN": "正在诊断文件格式…",
    "zh-TW": "正在診斷文件格式…",
    "en": "Inspecting EPUB format…",
    "ja": "EPUB 形式を診断中…",
    "ko": "EPUB 형식 진단 중…"
  },
  "ui_228": {
    "zh-CN": "正在读取任务及付款状态…",
    "zh-TW": "正在讀取任務及付款狀態…",
    "en": "Loading task and payment status…",
    "ja": "タスクと支払い状態を読み込み中…",
    "ko": "작업 및 결제 상태 불러오는 중…"
  },
  "ui_229": {
    "zh-CN": "支付已确认，正在后台修复文件，可以关闭页面。",
    "zh-TW": "支付已確認，正在後臺修復文件，可以關閉頁面。",
    "en": "Payment verified. Repair is running in the background; you can close this page.",
    "ja": "支払い確認済み。バックグラウンドで修復中です。ページを閉じてもかまいません。",
    "ko": "결제가 확인되었습니다. 백그라운드에서 복구 중이므로 페이지를 닫아도 됩니다."
  },
  "ui_230": {
    "zh-CN": "尚未收到付款确认。完成付款后会在后台修复，可关闭页面稍后回来。",
    "zh-TW": "尚未收到付款確認。完成付款後會在後臺修復，可關閉頁面稍後回來。",
    "en": "Payment is not yet confirmed. After payment, repair runs in the background.",
    "ja": "支払いは未確認です。支払い後はバックグラウンドで修復します。",
    "ko": "아직 결제가 확인되지 않았습니다. 결제 후 백그라운드에서 복구됩니다."
  },
  "ui_231": {
    "zh-CN": "等待服务端确认付款。若已付款请勿重复支付，确认后会自动修复；可以关闭页面稍后回来。",
    "zh-TW": "等待服務端確認付款。若已付款請勿重複支付，確認後會自動修復；可以關閉頁面稍後回來。",
    "en": "Waiting for server verification. If you've paid, don't pay again. Repair starts automatically once verified.",
    "ja": "サーバーで支払いを確認中。支払い済みなら再度支払わないでください。確認後に自動修復します。",
    "ko": "서버에서 결제를 확인 중입니다. 이미 결제했다면 다시 결제하지 마세요. 확인 후 자동 복구합니다."
  },
  "ui_232": {
    "zh-CN": "正在核验 PayPal 付款，请勿重复付款…",
    "zh-TW": "正在覈驗 PayPal 付款，請勿重複付款…",
    "en": "Verifying PayPal payment. Please do not pay again…",
    "ja": "PayPal の支払いを確認中。再度支払わないでください…",
    "ko": "PayPal 결제 확인 중입니다. 다시 결제하지 마세요…"
  },
  "ui_233": {
    "zh-CN": "付款已由服务器核验。任务会在后台继续，请返回 FixEpub 查看进度和下载结果。",
    "zh-TW": "付款已由服務器覈驗。任務會在後臺繼續，請返回 FixEpub 查看進度和下載結果。",
    "en": "Payment verified by the server. Your task continues in the background. Return to FixEpub for progress and downloads.",
    "ja": "サーバーが支払いを確認しました。処理はバックグラウンドで続きます。FixEpub で進捗・結果を確認してください。",
    "ko": "서버에서 결제가 확인되었습니다. 작업은 백그라운드에서 계속됩니다. FixEpub에서 진행 상황과 결과를 확인하세요."
  },
  "ui_234": {
    "zh-CN": "此订单当前无需付款，请返回任务中心查看状态。",
    "zh-TW": "此訂單當前無需付款，請返回任務中心查看狀態。",
    "en": "No payment is needed for this order now. Check its status in My tasks.",
    "ja": "この注文は現在支払い不要です。タスク一覧で状態を確認してください。",
    "ko": "이 주문은 현재 결제가 필요하지 않습니다. 작업 목록에서 상태를 확인하세요."
  },
  "ui_235": {
    "zh-CN": "已取消本次付款。原订单仍可在有效期内继续支付。",
    "zh-TW": "已取消本次付款。原訂單仍可在有效期內繼續支付。",
    "en": "Payment cancelled. You can resume the original order while it remains valid.",
    "ja": "支払いをキャンセルしました。有効期間内は元の注文で支払いを再開できます。",
    "ko": "결제가 취소되었습니다. 유효 기간 내에 원래 주문의 결제를 계속할 수 있습니다."
  },
  "ui_236": {
    "zh-CN": "PayPal 暂未完成付款。若已扣款，请点击重新核验；请勿重复付款。",
    "zh-TW": "PayPal 暫未完成付款。若已扣款，請點擊重新覈驗；請勿重複付款。",
    "en": "PayPal payment is not confirmed. If charged, select verify again; do not pay twice.",
    "ja": "PayPal の支払いは未確認です。引き落とし済みなら再確認し、二重に支払わないでください。",
    "ko": "PayPal 결제가 확인되지 않았습니다. 이미 청구되었다면 다시 확인을 선택하고 중복 결제하지 마세요."
  },
  "ui_237": {
    "zh-CN": "电子书格式转换",
    "zh-TW": "電子書格式轉換",
    "en": "Ebook conversion",
    "ja": "電子書籍変換",
    "ko": "전자책 변환"
  },
  "ui_238": {
    "zh-CN": "格式转换与用词精校",
    "zh-TW": "格式轉換與用詞精校",
    "en": "Conversion and wording review",
    "ja": "変換・用語校正",
    "ko": "변환 및 용어 교정"
  },
  "ui_239": {
    "zh-CN": "本",
    "zh-TW": "本",
    "en": "book(s)",
    "ja": "冊",
    "ko": "권"
  },
  "ui_240": {
    "zh-CN": "专业级电子书排版与翻译工具：一键上传 EPUB，获取极致双语对照、完美竖排转横排、Kindle/Apple Books 原生级阅读体验。",
    "zh-TW": "專業級電子書排版與翻譯工具：一鍵上傳 EPUB，獲取極致雙語對照、完美豎排轉橫排、Kindle/Apple Books 原生級閱讀體驗。",
    "en": "Translate and format ebooks, create bilingual editions, convert vertical layouts, and optimize for Kindle or Apple Books.",
    "ja": "電子書籍を翻訳・組版し、対訳版の作成、横書き変換、Kindle・Apple Books 向け最適化ができます。",
    "ko": "전자책 번역·서식, 대역 제작, 가로쓰기 변환 및 Kindle·Apple Books 최적화를 제공합니다."
  },
  "ui_241": {
    "zh-CN": "仅支持可可靠处理的文字型 PDF。保留原文和原图，不翻译、不 OCR、不做 用词精校，也不删除原书内容。复杂版式或不支持的图片会明确拒绝，不会强行生成。",
    "zh-TW": "僅支持可可靠處理的文字型 PDF。保留原文和原圖，不翻譯、不 OCR、不做 用詞精校，也不刪除原書內容。複雜版式或不支持的圖片會明確拒絕，不會強行生成。",
    "en": "Text-layer PDFs only. Keeps original text and images; no translation, OCR, wording review or content deletion. Unsupported layouts or images are rejected.",
    "ja": "文字層付き PDF のみ。原文・画像を保持し、翻訳・OCR・用語校正・内容削除は行いません。未対応のレイアウトや画像は拒否します。",
    "ko": "텍스트 레이어 PDF 전용. 원문과 이미지를 유지하며 번역, OCR, 용어 교정, 내용 삭제는 하지 않습니다. 미지원 레이아웃이나 이미지는 거부됩니다."
  },
  "ui_242": {
    "zh-CN": "本次只将 PDF 转为 EPUB：保留原文与原图，不翻译、不 OCR、不精校、不删除内容。原稿的复杂页面可能与原版布局不同，请阅读以下检查结果。",
    "zh-TW": "本次只將 PDF 轉爲 EPUB：保留原文與原圖，不翻譯、不 OCR、不精校、不刪除內容。原稿的複雜頁面可能與原版佈局不同，請閱讀以下檢查結果。",
    "en": "This converts PDF to EPUB while retaining text and images. No translation, OCR, review or content deletion. Complex pages may look different; review the checks below.",
    "ja": "原文・画像を保持して PDF を EPUB に変換します。翻訳・OCR・校正・内容削除は行いません。複雑なページは見た目が変わる場合があります。以下を確認してください。",
    "ko": "원문과 이미지를 유지하며 PDF를 EPUB로 변환합니다. 번역, OCR, 교정, 내용 삭제는 하지 않습니다. 복잡한 페이지의 배치는 달라질 수 있으니 아래 결과를 확인하세요."
  },
  "ui_243": {
    "zh-CN": "⚠️ 引擎已降级为安全模式（仅保留排版方向转换），完整功能可尝试重新提交。",
    "zh-TW": "⚠️ 引擎已降級爲安全模式（僅保留排版方向轉換），完整功能可嘗試重新提交。",
    "en": "⚠️ Safe mode: only layout direction changes are retained. Retry for full processing.",
    "ja": "⚠️ セーフモード：組方向の変換のみ保持。全処理には再試行してください。",
    "ko": "⚠️ 안전 모드: 쓰기 방향 변환만 유지됩니다. 전체 처리를 위해 다시 시도하세요."
  },
  "ui_244": {
    "zh-CN": "翻译失败、排版异常或订单支付遇到问题，我们都会一对一跟进，结合您的文件复现、定位原因，并在修复后回归验证。",
    "zh-TW": "翻譯失敗、排版異常或訂單支付遇到問題，我們都會一對一跟進，結合您的文件復現、定位原因，並在修復後迴歸驗證。",
    "en": "Contact support about translation, formatting or payment issues. We can investigate using your file and verify fixes.",
    "ja": "翻訳・組版・支払いの問題はサポートへ。ファイルを基に原因を調査し、修正後に確認します。",
    "ko": "번역, 서식 또는 결제 문제는 지원팀에 문의하세요. 파일을 바탕으로 원인을 조사하고 수정 결과를 검증합니다."
  },
  "ui_245": {
    "zh-CN": "请提供订单号或任务 ID、问题描述和截图。请在资料页点击“加好友”。若未能打开 QQ，请复制号码，在 QQ 中搜索并添加好友。",
    "zh-TW": "請提供訂單號或任務 ID、問題描述和截圖。請在資料頁點擊“加好友”。若未能打開 QQ，請複製號碼，在 QQ 中搜索並添加好友。",
    "en": "Provide the order/task ID, a description and a screenshot. Add support from the QQ profile, or copy the ID and search in QQ.",
    "ja": "注文・タスクID、問題の説明、スクリーンショットをご用意ください。QQ プロフィールから追加するか、ID をコピーして検索してください。",
    "ko": "주문·작업 ID, 문제 설명, 스크린샷을 준비하세요. QQ 프로필에서 추가하거나 ID를 복사하여 QQ에서 검색하세요."
  },
  "ui_246": {
    "zh-CN": "保存中…",
    "zh-TW": "保存中…",
    "en": "Saving…",
    "ja": "保存中…",
    "ko": "저장 중…"
  },
  "ui_247": {
    "zh-CN": "已保存，谢谢反馈",
    "zh-TW": "已保存，謝謝反饋",
    "en": "Saved. Thank you for your feedback.",
    "ja": "保存しました。ご意見ありがとうございます。",
    "ko": "저장되었습니다. 의견 감사합니다."
  },
  "ui_248": {
    "zh-CN": "反馈暂未保存",
    "zh-TW": "反饋暫未保存",
    "en": "Feedback was not saved",
    "ja": "フィードバックを保存できませんでした",
    "ko": "피드백이 저장되지 않았습니다"
  },
  "ui_249": {
    "zh-CN": "请先选择文件",
    "zh-TW": "請先選擇文件",
    "en": "Choose a file first",
    "ja": "先にファイルを選択してください",
    "ko": "먼저 파일을 선택하세요"
  },
  "ui_250": {
    "zh-CN": "开始翻译",
    "zh-TW": "開始翻譯",
    "en": "Start translation",
    "ja": "翻訳を開始",
    "ko": "번역 시작"
  },
  "ui_251": {
    "zh-CN": "上传中…",
    "zh-TW": "上傳中…",
    "en": "Uploading…",
    "ja": "アップロード中…",
    "ko": "업로드 중…"
  },
  "ui_252": {
    "zh-CN": "付款成功后，任务会在后台继续处理，可以关闭页面。稍后或隔天用",
    "zh-TW": "付款成功後，任務會在後臺繼續處理，可以關閉頁面。稍後或隔天用",
    "en": "After payment, processing continues in the background. You may close this page and return later using the",
    "ja": "支払い後はバックグラウンドで処理されます。ページを閉じて、後で",
    "ko": "결제 후 백그라운드에서 처리됩니다. 페이지를 닫고 나중에"
  },
  "ui_253": {
    "zh-CN": "同一浏览器",
    "zh-TW": "同一瀏覽器",
    "en": "same browser",
    "ja": "同じブラウザ",
    "ko": "같은 브라우저"
  },
  "ui_254": {
    "zh-CN": "回到本页、刷新查看进度，完成后即可下载；请勿清理本站记录。处理时间取决于书籍大小和排队情况。",
    "zh-TW": "回到本頁、刷新查看進度，完成後即可下載；請勿清理本站記錄。處理時間取決於書籍大小和排隊情況。",
    "en": "to refresh progress and download when ready. Keep this site's browser data. Processing time depends on book size and queue length.",
    "ja": "でこのページを開き、進捗の確認とダウンロードができます。サイトデータを削除しないでください。所要時間は書籍のサイズと待ち状況によります。",
    "ko": "로 돌아와 진행 상황을 확인하고 완료 후 다운로드하세요. 사이트 데이터를 삭제하지 마세요. 처리 시간은 책 크기와 대기열에 따라 달라집니다."
  },
  "ui_255": {
    "zh-CN": "你的邮箱地址",
    "zh-TW": "你的郵箱地址",
    "en": "Your email address",
    "ja": "メールアドレス",
    "ko": "이메일 주소"
  },
  "ui_256": {
    "zh-CN": "正在检查邮件通知服务…",
    "zh-TW": "正在檢查郵件通知服務…",
    "en": "Checking email notifications…",
    "ja": "メール通知を確認中…",
    "ko": "이메일 알림 확인 중…"
  },
  "ui_257": {
    "zh-CN": "仅用于当前任务的结果通知，邮件中提供下载入口。留空并保存可取消通知。",
    "zh-TW": "僅用於當前任務的結果通知，郵件中提供下載入口。留空並保存可取消通知。",
    "en": "Used only for this task's result notification and download link. Save an empty address to cancel notifications.",
    "ja": "このタスクの結果通知とダウンロードリンクのみに使用します。空欄で保存すると通知を解除します。",
    "ko": "이 작업의 결과 알림과 다운로드 링크에만 사용됩니다. 빈 주소로 저장하면 알림이 취소됩니다."
  },
  "ui_258": {
    "zh-CN": "浏览器未允许保存任务记录，关闭后无法保证恢复。建议先保存邮箱，收到通知后从邮件进入下载。",
    "zh-TW": "瀏覽器未允許保存任務記錄，關閉後無法保證恢復。建議先保存郵箱，收到通知後從郵件進入下載。",
    "en": "Your browser cannot save task history. Save your email to receive a download link before closing.",
    "ja": "ブラウザに履歴を保存できません。閉じる前にメールを保存し、通知のリンクからダウンロードしてください。",
    "ko": "브라우저에 작업 기록을 저장할 수 없습니다. 닫기 전에 이메일을 저장하여 다운로드 링크를 받으세요."
  },
  "ui_259": {
    "zh-CN": "邮件服务尚未启用，请用同一浏览器回来查看和下载；已有通知可清空邮箱后保存取消。",
    "zh-TW": "郵件服務尚未啓用，請用同一瀏覽器回來查看和下載；已有通知可清空郵箱後保存取消。",
    "en": "Email notifications are unavailable. Return with the same browser to download. Clear and save an existing address to cancel its notifications.",
    "ja": "メール通知は利用できません。同じブラウザで戻ってダウンロードしてください。既存の通知はメールを空欄で保存すると解除できます。",
    "ko": "이메일 알림을 사용할 수 없습니다. 같은 브라우저로 돌아와 다운로드하세요. 기존 주소를 비우고 저장하면 알림이 취소됩니다."
  },
  "ui_260": {
    "zh-CN": "暂时无法确认邮件服务，请用同一浏览器回来查看和下载；已有通知可清空邮箱后保存取消。",
    "zh-TW": "暫時無法確認郵件服務，請用同一瀏覽器回來查看和下載；已有通知可清空郵箱後保存取消。",
    "en": "Cannot check email service. Return with the same browser to download; clear and save your address to cancel notifications.",
    "ja": "メールサービスを確認できません。同じブラウザで戻ってダウンロードしてください。空欄で保存すると通知を解除できます。",
    "ko": "이메일 서비스를 확인할 수 없습니다. 같은 브라우저로 돌아와 다운로드하세요. 주소를 비우고 저장하면 알림이 취소됩니다."
  },
  "ui_261": {
    "zh-CN": "可先填写邮箱，创建任务后自动保存；已有任务请点击保存。",
    "zh-TW": "可先填寫郵箱，創建任務後自動保存；已有任務請點擊保存。",
    "en": "Enter your email now to save it with the next task, or click Save for an existing task.",
    "ja": "先にメールを入力すると新規タスクに保存されます。既存タスクは保存を押してください。",
    "ko": "이메일을 입력하면 새 작업에 자동 저장됩니다. 기존 작업은 저장을 누르세요."
  },
  "ui_262": {
    "zh-CN": "未设置邮件通知；仍可回到本页查看任务。",
    "zh-TW": "未設置郵件通知；仍可回到本頁查看任務。",
    "en": "Email notifications are off. You can still return to view this task.",
    "ja": "メール通知は未設定です。このページでタスクを確認できます。",
    "ko": "이메일 알림이 꺼져 있습니다. 이 페이지에서 작업을 확인할 수 있습니다."
  },
  "ui_263": {
    "zh-CN": "结果邮件已发送，请检查收件箱和垃圾邮件。",
    "zh-TW": "結果郵件已發送，請檢查收件箱和垃圾郵件。",
    "en": "Result email sent. Check your inbox and spam folder.",
    "ja": "結果メールを送信しました。受信箱と迷惑メールをご確認ください。",
    "ko": "결과 이메일을 보냈습니다. 받은편지함과 스팸함을 확인하세요."
  },
  "ui_264": {
    "zh-CN": "邮箱已保存，但邮件暂未发送成功；请回到本页下载。",
    "zh-TW": "郵箱已保存，但郵件暫未發送成功；請回到本頁下載。",
    "en": "Email saved, but delivery failed. Return here to download.",
    "ja": "メールは保存済みですが送信できませんでした。このページでダウンロードしてください。",
    "ko": "이메일은 저장되었지만 전송하지 못했습니다. 여기로 돌아와 다운로드하세요."
  },
  "ui_265": {
    "zh-CN": "邮箱已保存，完成后将发送结果通知。",
    "zh-TW": "郵箱已保存，完成後將發送結果通知。",
    "en": "Email saved. We will notify you when ready.",
    "ja": "メールを保存しました。完了時に通知します。",
    "ko": "이메일을 저장했습니다. 완료되면 알려드립니다."
  },
  "ui_266": {
    "zh-CN": "已填写邮箱，上传创建任务后会自动保存。",
    "zh-TW": "已填寫郵箱，上傳創建任務後會自動保存。",
    "en": "Your email will be saved when you upload a file.",
    "ja": "アップロード時にメールを保存します。",
    "ko": "파일을 업로드하면 이메일이 저장됩니다."
  },
  "ui_267": {
    "zh-CN": "正在保存邮箱…",
    "zh-TW": "正在保存郵箱…",
    "en": "Saving email…",
    "ja": "メールを保存中…",
    "ko": "이메일 저장 중…"
  },
  "ui_268": {
    "zh-CN": "邮箱未保存成功，请重试保存；任务处理不受影响。",
    "zh-TW": "郵箱未保存成功，請重試保存；任務處理不受影響。",
    "en": "Could not save email. Try again; processing is unaffected.",
    "ja": "メールを保存できませんでした。再試行してください。処理には影響しません。",
    "ko": "이메일을 저장하지 못했습니다. 다시 시도하세요. 작업 처리에는 영향이 없습니다."
  },
  "ui_269": {
    "zh-CN": "邮箱选填，上传后自动保存到新任务。",
    "zh-TW": "郵箱選填，上傳後自動保存到新任務。",
    "en": "Email is optional and saved with your next upload.",
    "ja": "メールは任意です。次回のアップロード時に保存します。",
    "ko": "이메일은 선택 사항이며 다음 업로드에 저장됩니다."
  },
  "ui_270": {
    "zh-CN": "邮件服务尚未启用，可回到本页下载。",
    "zh-TW": "郵件服務尚未啓用，可回到本頁下載。",
    "en": "Email is unavailable. Return here to download.",
    "ja": "メールは利用できません。このページでダウンロードできます。",
    "ko": "이메일을 사용할 수 없습니다. 이 페이지에서 다운로드하세요."
  },
  "ui_271": {
    "zh-CN": "暂时无法读取邮箱设置；任务仍可在本页查看。",
    "zh-TW": "暫時無法讀取郵箱設置；任務仍可在本頁查看。",
    "en": "Cannot load email settings. Your task remains available here.",
    "ja": "メール設定を読み込めません。このページでタスクを確認できます。",
    "ko": "이메일 설정을 불러올 수 없습니다. 이 페이지에서 작업을 확인하세요."
  },
  "ui_272": {
    "zh-CN": "选择文件后开始",
    "zh-TW": "選擇文件後開始",
    "en": "Choose a file to start",
    "ja": "ファイルを選択して開始",
    "ko": "파일을 선택하여 시작"
  },
  "ui_273": {
    "zh-CN": "繁体（竖/横排）→ 横排简体",
    "zh-TW": "繁體（豎/橫排）→ 橫排簡體",
    "en": "Traditional → horizontal Simplified",
    "ja": "繁体字 → 横書き簡体字",
    "ko": "번체 → 가로쓰기 간체"
  },
  "ui_274": {
    "zh-CN": "横排简体 → 横排繁体",
    "zh-TW": "橫排簡體 → 橫排繁體",
    "en": "Simplified → horizontal Traditional",
    "ja": "簡体字 → 横書き繁体字",
    "ko": "간체 → 가로쓰기 번체"
  },
  "ui_275": {
    "zh-CN": "等待支付",
    "zh-TW": "等待支付",
    "en": "Awaiting payment",
    "ja": "支払い待ち",
    "ko": "결제 대기"
  },
  "ui_276": {
    "zh-CN": "等待处理",
    "zh-TW": "等待處理",
    "en": "Waiting",
    "ja": "処理待ち",
    "ko": "처리 대기"
  },
  "ui_277": {
    "zh-CN": "待确认",
    "zh-TW": "待確認",
    "en": "Awaiting confirmation",
    "ja": "確認待ち",
    "ko": "확인 대기"
  },
  "ui_278": {
    "zh-CN": "排队中",
    "zh-TW": "排隊中",
    "en": "Queued",
    "ja": "待機中",
    "ko": "대기열"
  },
  "ui_279": {
    "zh-CN": "处理中",
    "zh-TW": "處理中",
    "en": "Processing",
    "ja": "処理中",
    "ko": "처리 중"
  },
  "ui_280": {
    "zh-CN": "修复中",
    "zh-TW": "修復中",
    "en": "Repairing",
    "ja": "修復中",
    "ko": "복구 중"
  },
  "ui_281": {
    "zh-CN": "已完成",
    "zh-TW": "已完成",
    "en": "Completed",
    "ja": "完了",
    "ko": "완료"
  },
  "ui_282": {
    "zh-CN": "已修复",
    "zh-TW": "已修復",
    "en": "Repaired",
    "ja": "修復済み",
    "ko": "복구 완료"
  },
  "ui_283": {
    "zh-CN": "部分完成",
    "zh-TW": "部分完成",
    "en": "Partially completed",
    "ja": "一部完了",
    "ko": "일부 완료"
  },
  "ui_284": {
    "zh-CN": "失败",
    "zh-TW": "失敗",
    "en": "Failed",
    "ja": "失敗",
    "ko": "실패"
  },
  "ui_285": {
    "zh-CN": "已停止",
    "zh-TW": "已停止",
    "en": "Stopped",
    "ja": "停止済み",
    "ko": "중지됨"
  },
  "ui_286": {
    "zh-CN": "查看最新状态",
    "zh-TW": "查看最新狀態",
    "en": "View current status",
    "ja": "最新状況を表示",
    "ko": "최신 상태 보기"
  },
  "ui_287": {
    "zh-CN": "查看 / 下载",
    "zh-TW": "查看 / 下載",
    "en": "View / Download",
    "ja": "表示 / ダウンロード",
    "ko": "보기 / 다운로드"
  },
  "ui_288": {
    "zh-CN": "全部完成",
    "zh-TW": "全部完成",
    "en": "All completed",
    "ja": "すべて完了",
    "ko": "모두 완료"
  },
  "ui_289": {
    "zh-CN": "全部失败",
    "zh-TW": "全部失敗",
    "en": "All failed",
    "ja": "すべて失敗",
    "ko": "모두 실패"
  },
  "ui_290": {
    "zh-CN": "批量处理中",
    "zh-TW": "批量處理中",
    "en": "Processing batch",
    "ja": "一括処理中",
    "ko": "일괄 처리 중"
  },
  "ui_291": {
    "zh-CN": "已取消",
    "zh-TW": "已取消",
    "en": "Cancelled",
    "ja": "キャンセル済み",
    "ko": "취소됨"
  },
  "ui_292": {
    "zh-CN": "停止中...",
    "zh-TW": "停止中...",
    "en": "Stopping…",
    "ja": "停止中…",
    "ko": "중지 중…"
  },
  "ui_293": {
    "zh-CN": "排队中...",
    "zh-TW": "排隊中...",
    "en": "Queueing…",
    "ja": "待機列に追加中…",
    "ko": "대기열에 추가 중…"
  },
  "ui_294": {
    "zh-CN": "正在排队...",
    "zh-TW": "正在排隊...",
    "en": "Queueing…",
    "ja": "待機列に追加中…",
    "ko": "대기열에 추가 중…"
  },
  "ui_295": {
    "zh-CN": "正在查询订单…",
    "zh-TW": "正在查詢訂單…",
    "en": "Checking order…",
    "ja": "注文を確認中…",
    "ko": "주문 확인 중…"
  },
  "ui_296": {
    "zh-CN": "正在创建订单...",
    "zh-TW": "正在創建訂單...",
    "en": "Creating order…",
    "ja": "注文を作成中…",
    "ko": "주문 생성 중…"
  },
  "ui_297": {
    "zh-CN": "翻译进度",
    "zh-TW": "翻譯進度",
    "en": "Translation progress",
    "ja": "翻訳の進捗",
    "ko": "번역 진행 상황"
  },
  "ui_298": {
    "zh-CN": "↓ 下载翻译结果",
    "zh-TW": "↓ 下載翻譯結果",
    "en": "↓ Download translation",
    "ja": "↓ 翻訳をダウンロード",
    "ko": "↓ 번역 다운로드"
  },
  "ui_299": {
    "zh-CN": "↓ 下载转换结果",
    "zh-TW": "↓ 下載轉換結果",
    "en": "↓ Download converted book",
    "ja": "↓ 変換結果をダウンロード",
    "ko": "↓ 변환 결과 다운로드"
  },
  "ui_300": {
    "zh-CN": "批量转换进度",
    "zh-TW": "批量轉換進度",
    "en": "Batch conversion progress",
    "ja": "一括変換の進捗",
    "ko": "일괄 변환 진행 상황"
  },
  "ui_301": {
    "zh-CN": "↓ 下载批次 ZIP",
    "zh-TW": "↓ 下載批次 ZIP",
    "en": "↓ Download batch ZIP",
    "ja": "↓ 一括 ZIP をダウンロード",
    "ko": "↓ 일괄 ZIP 다운로드"
  },
  "ui_302": {
    "zh-CN": "支付成功后会在后台自动翻译，可以关闭本页，稍后回来查看",
    "zh-TW": "支付成功後會在後臺自動翻譯，可以關閉本頁，稍後回來查看",
    "en": "Translation continues after payment. You can close this page and return later.",
    "ja": "支払い後はバックグラウンドで翻訳します。ページを閉じて後で確認できます。",
    "ko": "결제 후 백그라운드에서 번역합니다. 페이지를 닫고 나중에 확인하세요."
  },
  "ui_303": {
    "zh-CN": "支付成功后会在后台自动转换，可以关闭本页，稍后回来查看",
    "zh-TW": "支付成功後會在後臺自動轉換，可以關閉本頁，稍後回來查看",
    "en": "Conversion continues after payment. You can close this page and return later.",
    "ja": "支払い後はバックグラウンドで変換します。ページを閉じて後で確認できます。",
    "ko": "결제 후 백그라운드에서 변환합니다. 페이지를 닫고 나중에 확인하세요."
  },
  "locale_label": {
    "zh-CN": "界面语言",
    "zh-TW": "介面語言",
    "en": "Interface language",
    "ja": "表示言語",
    "ko": "화면 언어"
  },
  "theme_label": {
    "zh-CN": "外观",
    "zh-TW": "外觀",
    "en": "Appearance",
    "ja": "外観",
    "ko": "테마"
  },
  "system": {
    "zh-CN": "跟随系统",
    "zh-TW": "跟隨系統",
    "en": "System",
    "ja": "システム",
    "ko": "시스템"
  },
  "light": {
    "zh-CN": "浅色",
    "zh-TW": "淺色",
    "en": "Light",
    "ja": "ライト",
    "ko": "라이트"
  },
  "dark": {
    "zh-CN": "深色",
    "zh-TW": "深色",
    "en": "Dark",
    "ja": "ダーク",
    "ko": "다크"
  },
  "overview": {
    "zh-CN": "电子书处理服务",
    "zh-TW": "電子書處理服務",
    "en": "Ebook processing",
    "ja": "電子書籍処理",
    "ko": "전자책 처리"
  },
  "overview_body": {
    "zh-CN": "支持格式转换、繁简转换、五种语言互译与 EPUB 修复。上传后查看报价，付款后后台处理。文本 PDF 使用独立入口，不包含 OCR。",
    "zh-TW": "支援格式轉換、繁簡轉換、五種語言互譯與 EPUB 修復。上傳後查看報價，付款後背景處理。文字 PDF 使用獨立入口，不包含 OCR。",
    "en": "Convert ebook formats and Chinese scripts, translate between five languages, or repair EPUBs. Review the quote after upload; paid tasks continue in the background. Text-layer PDFs use a separate workflow, without OCR.",
    "ja": "電子書籍形式・中国語字体の変換、5言語の相互翻訳、EPUB 修復に対応。アップロード後に見積もりを確認し、支払い後はバックグラウンド処理。文字層付き PDF は専用フローで、OCR は含みません。",
    "ko": "전자책 형식 및 중국어 문자 변환, 5개 언어 상호 번역, EPUB 복구를 지원합니다. 업로드 후 견적을 확인하면 결제 후 백그라운드에서 처리됩니다. 텍스트 레이어가 있는 PDF는 별도 도구를 사용하며 OCR은 포함되지 않습니다."
  },
  "support_notice": {
    "zh-CN": "技术诊断与法律原文保留原语言。",
    "zh-TW": "技術診斷與法律原文保留原語言。",
    "en": "Technical diagnostics and legal originals retain their original language.",
    "ja": "技術診断と法的文書の原文は元の言語で表示します。",
    "ko": "기술 진단 및 법률 원문은 원래 언어로 표시됩니다."
  },
  "pdf_limits": {
    "zh-CN": "单文件不超过 {size} MB / {pages} 页。先检查并确认风险，再支付 {price}；不支持批量或文件夹。",
    "zh-TW": "單檔不超過 {size} MB / {pages} 頁。先檢查並確認風險，再支付 {price}；不支援批次或資料夾。",
    "en": "One PDF, up to {size} MB / {pages} pages. Review risks before paying {price}. No batches or folders.",
    "ja": "PDF 1冊、最大 {size} MB / {pages} ページ。リスク確認後に {price} を支払います。一括・フォルダーは非対応。",
    "ko": "PDF 1개, 최대 {size} MB / {pages}페이지. 위험을 확인한 후 {price}를 결제하세요. 일괄 또는 폴더 업로드는 지원하지 않습니다."
  },
  "polish_language": {
    "zh-CN": "精校正文语言",
    "zh-TW": "精校正文語言",
    "en": "Book language for proofreading",
    "ja": "校正する本文の言語",
    "ko": "교정할 본문 언어"
  },
  "polish_preserve": {
    "zh-CN": "选择书稿当前语言；精校不翻译。繁体、英、日、韩精校保留原语言，不执行上方繁简转换。",
    "zh-TW": "選擇書稿目前語言；精校不翻譯。繁體、英、日、韓精校保留原語言，不執行上方繁簡轉換。",
    "en": "Select the book’s current language. Proofreading does not translate. Traditional Chinese, English, Japanese and Korean skip the Chinese conversion setting above.",
    "ja": "本文の現在の言語を選択してください。校正は翻訳しません。繁体字・英語・日本語・韓国語では上の繁簡変換設定を適用しません。",
    "ko": "책의 현재 언어를 선택하세요. 교정은 번역하지 않습니다. 번체·영어·일본어·한국어는 위의 중국어 변환 설정을 적용하지 않습니다."
  },
  "review_open": {
    "zh-CN": "查看精校修改对照",
    "zh-TW": "查看精校修改對照",
    "en": "Review proofreading changes",
    "ja": "校正の変更を確認",
    "ko": "교정 변경 비교"
  },
  "review_notice": {
    "zh-CN": "勾选保留修改，取消则使用精校前文字。仅影响本次下载，不覆盖原成品；关闭窗口后重置选择。对照不包含基础繁简转换和排版修改。",
    "zh-TW": "勾選保留修改，取消則使用精校前文字。僅影響本次下載，不覆蓋原成品；關閉視窗後重設選擇。對照不含基礎繁簡轉換和排版修改。",
    "en": "Keep checked edits; uncheck to restore the pre-proofreading text. Applies only to this download, not the original result. Closing resets selections. Chinese conversion and layout changes are not included.",
    "ja": "チェックを外すと校正前の文章に戻します。今回のダウンロードのみ対象で、元の成果物は変更しません。閉じると選択をリセットします。繁簡変換・レイアウトの変更は含みません。",
    "ko": "체크를 해제하면 교정 전 문장을 사용합니다. 이번 다운로드에만 적용되며 원본 결과는 유지됩니다. 닫으면 선택이 초기화됩니다. 중국어 변환과 서식 변경은 비교에 포함되지 않습니다."
  },
  "review_close": {
    "zh-CN": "关闭",
    "zh-TW": "關閉",
    "en": "Close",
    "ja": "閉じる",
    "ko": "닫기"
  },
  "review_previous": {
    "zh-CN": "上一页",
    "zh-TW": "上一頁",
    "en": "Previous",
    "ja": "前へ",
    "ko": "이전"
  },
  "review_next": {
    "zh-CN": "下一页",
    "zh-TW": "下一頁",
    "en": "Next",
    "ja": "次へ",
    "ko": "다음"
  },
  "review_download": {
    "zh-CN": "下载所选版本 EPUB",
    "zh-TW": "下載所選版本 EPUB",
    "en": "Download selected EPUB",
    "ja": "選択した EPUB をダウンロード",
    "ko": "선택한 EPUB 다운로드"
  },
  "review_before": {
    "zh-CN": "修改前",
    "zh-TW": "修改前",
    "en": "Before",
    "ja": "変更前",
    "ko": "변경 전"
  },
  "review_after": {
    "zh-CN": "修改后",
    "zh-TW": "修改後",
    "en": "After",
    "ja": "変更後",
    "ko": "변경 후"
  },
  "review_keep": {
    "zh-CN": "保留此修改",
    "zh-TW": "保留此修改",
    "en": "Keep this change",
    "ja": "この変更を採用",
    "ko": "이 변경 유지"
  },
  "review_loading": {
    "zh-CN": "正在加载或生成，请稍候…",
    "zh-TW": "正在載入或產生，請稍候…",
    "en": "Loading or preparing your file…",
    "ja": "読み込み・生成中…",
    "ko": "불러오거나 생성하는 중…"
  },
  "review_error": {
    "zh-CN": "无法读取修改对照",
    "zh-TW": "無法讀取修改對照",
    "en": "Unable to load changes",
    "ja": "変更を読み込めません",
    "ko": "변경을 불러올 수 없습니다"
  },
  "review_empty": {
    "zh-CN": "已完成检查，无需修改正文。",
    "zh-TW": "已完成檢查，無需修改正文。",
    "en": "Review complete; no text changes needed.",
    "ja": "確認完了。本文の変更はありません。",
    "ko": "검토 완료. 본문 변경이 없습니다."
  },
  "review_saved": {
    "zh-CN": "所选版本已开始下载，原成品保持不变。",
    "zh-TW": "所選版本已開始下載，原成品保持不變。",
    "en": "Download started. The original result is unchanged.",
    "ja": "ダウンロードを開始しました。元の成果物は変更していません。",
    "ko": "다운로드를 시작했습니다. 원본 결과는 유지됩니다."
  },
  "txt_notice": {
    "zh-CN": "TXT 支持 UTF-8 或带 BOM 的 UTF-16；按常见章标题生成目录。普通转换保留原语言，输出 EPUB；翻译与精校另计。",
    "zh-TW": "TXT 支援 UTF-8 或帶 BOM 的 UTF-16；依常見章標題產生目錄。一般轉換保留原語言，輸出 EPUB；翻譯與精校另計。",
    "en": "TXT accepts UTF-8 or UTF-16 with BOM. Common chapter headings create a contents list. Basic conversion preserves the language and outputs EPUB; translation and proofreading are priced separately.",
    "ja": "TXT は UTF-8 または BOM 付き UTF-16 に対応。一般的な章見出しから目次を作成します。通常変換は原言語の EPUB を出力し、翻訳・校正は別料金です。",
    "ko": "TXT는 UTF-8 또는 BOM이 있는 UTF-16을 지원합니다. 일반적인 장 제목으로 목차를 만듭니다. 기본 변환은 원어 EPUB을 출력하며 번역·교정은 별도 요금입니다."
  }
};
if(typeof module === "object" && module.exports) module.exports=messages; else root.FixEpubMessages=messages;
})(typeof window !== "undefined" ? window : globalThis);
