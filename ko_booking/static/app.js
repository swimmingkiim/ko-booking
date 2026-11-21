"use strict";

const $ = (selector, root = document) => root.querySelector(selector);
const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const money = value => new Intl.NumberFormat("ko-KR").format(BigInt(String(value || "0")));
const decimalMoney = value => {const [whole,fraction]=String(value).split(".");return money(whole)+(fraction?"."+fraction:"");};
const transactionMoney = tx => {const amount=BigInt(tx.amount_krw),effective=tx.direction==="income"?amount:-amount;return (effective<0n?"−":"+")+money(amount<0n?-amount:amount);};
const icon = name => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true">${({plus:'<path d="M12 5v14M5 12h14"/>',book:'<rect x="5" y="3" width="15" height="18" rx="3"/><path d="M9 3v18M12 8h5M12 12h5"/>',search:'<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',income:'<path d="M4 17 10 11l4 3 6-8M15 6h5v5"/>',expense:'<rect x="3" y="5" width="18" height="14" rx="3"/><path d="M3 10h18M7 15h4"/>',asset:'<rect x="4" y="4" width="16" height="13" rx="2"/><path d="M8 21h8M12 17v4"/>',balance:'<path d="M5 8h14M5 16h14M8 4 4 8l4 4M16 12l4 4-4 4"/>'})[name] || ''}</svg>`;
const viewNames = {overview:"장부 한눈에",transactions:"거래 검토",import:"자료 가져오기",ledger:"간편장부",tax:"세금·근거"};
const vatModes = {unconfirmed:"확인 전",general:"일반과세자",simplified:"간이과세자",exempt:"면세사업자"};
const stepNames = {basic_income_tax:"과세표준에 기본세율 적용",general_income:"기본율 구간의 소득",excess_income:"초과율 구간의 소득",standard_income:"주요경비와 기준경비율 반영",comparison_income:"단순경비율 소득 × 비교 배율",selected_income:"두 산식 중 적은 소득금액"};
let state, view = "overview", upload = null, preview = null, activeTransaction = null;
let transactionFilter = "all", search = "", toastTimer;

async function api(path, data) {
  const response = await fetch(path, data === undefined ? {} : {method:"POST",headers:{"Content-Type":"application/json","X-Ko-Booking":"local-app"},body:JSON.stringify(data)});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "요청을 처리하지 못했습니다.");
  return result;
}
function toast(message) {
  const element = $("#toast"); element.textContent = message; element.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => element.hidden = true, 5000);
}
async function refresh() { state = await api("/api/state"); render(); }
function year() { return state.settings.year; }
function selectedTransactions() { return state.transactions.filter(tx => Number(tx.date.slice(0,4)) === year()); }
function status(tx) {
  if (tx.link_kind === "duplicate") return '<span class="status">중복 연결</span>';
  if (state.summary.pending_ids.includes(tx.id)) return `<span class="status amber">${BigInt(tx.amount_krw) < 0n && !tx.link_kind ? "환불 연결 필요" : "분류 확인"}</span>`;
  if (state.summary.vat_pending_ids.includes(tx.id)) return '<span class="status amber">부가세 확인</span>';
  if (tx.allocations.every(p => ["개인거래","자금이동"].includes(p.category))) return '<span class="status">집계 제외</span>';
  return '<span class="status green">검토됨</span>';
}
function heading(title, description, action = "") {
  return `<div class="page-heading"><div><div class="eyebrow">MY BUSINESS RECORD</div><h1>${title}</h1><p>${description}</p></div><div class="actions">${action}</div></div>`;
}
const importButton = `<a class="button primary" href="#import">${icon("plus")}자료 가져오기</a>`;
function reviewNotice() {
  const s = state.summary, pending = new Set([...s.pending_ids,...s.vat_pending_ids]).size;
  if (!s.count) return `<div class="notice"><p><strong>첫 장부를 시작해 볼까요?</strong> 거래 자료를 가져오면 한 해의 흐름이 보여요.</p><button class="button quiet" data-action="demo">예시로 둘러보기</button></div>`;
  if (!state.settings.vat_mode || state.settings.vat_mode==="unconfirmed") return `<div class="notice warning"><p><strong>${year()}년 과세유형을 먼저 확인해 주세요.</strong> 수입의 부가세 구분 방식이 달라요.</p><button class="button quiet" data-action="open-profile">사업장 설정</button></div>`;
  if (s.reviewed) return `<div class="notice success"><p><strong>${year()}년 자료 확인을 마쳤어요.</strong> 검토한 장부를 내려받을 수 있습니다.</p><a class="button quiet" href="#ledger">간편장부 보기</a></div>`;
  return `<div class="notice ${pending ? "warning" : ""}"><p><strong>${pending ? `${pending}건을 더 확인해 주세요.` : "거래 정리가 끝나가고 있어요."}</strong> ${pending ? "미분류·환불 연결·부가세 확인이 남아 있어요." : "선택 연도 자료가 모두 포함되었는지 확인해 주세요."}</p><a class="button quiet" href="${pending ? "#transactions" : "#ledger"}">자료 확인</a></div>`;
}
function metrics() {
  const s = state.summary;
  return `<div class="metrics">${[["income","총 수입",s.totals.income,"income","분류된 거래 · 부가세 포함"],["expense","총 비용",s.totals.expense,"expense","개인거래·자산취득 제외"],["asset","자산 증감",s.totals.asset,"asset","취득·매각 거래 기록"],["balance","수입 − 비용",s.difference_krw,"balance","공제·조정 전 단순 차액"]].map(([type,label,value,symbol,foot]) => `<article class="metric ${type}"><div class="metric-head">${label}<span class="metric-icon">${icon(symbol)}</span></div><div class="metric-value">${money(value)}<span>원</span></div><div class="metric-foot">${foot}</div></article>`).join("")}</div>`;
}
function chart() {
  if (!state.summary.rows.length) return '<div class="empty-chart">분류한 거래가 쌓이면 월별 흐름을 보여드려요.</div>';
  const months = state.summary.monthly;
  const abs = n => n < 0n ? -n : n;
  const maximum = months.reduce((max,m) => [abs(BigInt(m.income)),abs(BigInt(m.expense)),max].reduce((a,b)=>a>b?a:b),1n);
  return `<div class="chart"><svg viewBox="0 0 840 180" role="img" aria-label="${year()}년 월별 수입과 비용. 0선 아래는 취소·환불 순액입니다."><line x1="5" y1="25" x2="835" y2="25"/><line x1="5" y1="57" x2="835" y2="57"/><line x1="5" y1="92" x2="835" y2="92"/><line x1="5" y1="126" x2="835" y2="126"/>${months.map((m,i) => `${["income","expense"].map((kind,k)=>{const value=BigInt(m[kind]), h=Number(abs(value)*65n/maximum);return `<rect class="${kind}" x="${i*69+20+k*19}" y="${value>=0n ? 92-h : 92}" width="13" height="${h}" rx="3"><title>${m.month}월 ${kind==="income"?"수입":"비용"} ${money(m[kind])}원</title></rect>`;}).join("")}<text x="${i*69+37}" y="174" text-anchor="middle">${m.month}월</text>`).join("")}</svg></div>`;
}
function transactionsTable(transactions, recent = false) {
  if (!transactions.length) return `<div class="empty"><div class="empty-symbol">${icon("book")}</div><h2>${search ? "검색 결과가 없어요" : "아직 거래가 없어요"}</h2><p>${search ? "거래처·출처·분류를 다른 단어로 찾아보세요." : "은행·카드 거래를 CSV로 가져오고, 한 건씩 분류해 보세요."}</p>${search ? "" : importButton}</div>`;
  return `<div class="table-wrap"><table><thead><tr><th>거래처·내용</th><th>거래일</th><th>분류</th><th>확인 상태</th><th class="money">금액</th><th></th></tr></thead><tbody>${transactions.map(tx => `<tr data-transaction="${tx.id}"><td><div class="merchant"><span class="merchant-icon">${tx.direction==="income" ? "▧" : "▢"}</span><div><strong>${esc(tx.merchant)}</strong><small>${esc(tx.source)}</small></div></div></td><td class="muted">${esc(tx.date.slice(5).replace("-","."))}<small> · ${esc(tx.date.slice(0,4))}</small></td><td><span class="status ${tx.allocations[0].category ? "purple" : ""}">${tx.allocations.length>1 ? `${tx.allocations.length}개로 분할` : esc(tx.allocations[0].category || "미분류")}</span></td><td>${status(tx)}</td><td class="money ${tx.direction}">${transactionMoney(tx)}원</td><td><button class="button quiet compact" data-action="edit" data-id="${tx.id}" aria-label="${esc(tx.merchant)} 거래 검토">검토</button></td></tr>`).join("")}</tbody></table></div>`;
}
function overview() {
  const pending = state.summary.pending_ids.length, vat = state.summary.vat_pending_ids.length;
  return `${heading("내 사업의 흐름을 한눈에",`${year()}년, 하나씩 모아가는 사장님의 사업 기록`,importButton)}${reviewNotice()}${metrics()}<div class="grid-main"><section class="panel"><div class="panel-heading"><div><h2>월별 수입과 비용</h2><p>분류한 거래의 총액으로 흐름을 확인해요.</p></div><div class="legend"><span>수입</span><span>비용</span></div></div>${chart()}</section><section class="panel todo-panel"><h2>조금만 더 확인하면</h2><p class="todo-number">${new Set([...state.summary.pending_ids,...state.summary.vat_pending_ids]).size}<span>건</span></p><ul class="todo-list"><li>분류·환불 연결<strong>${pending}건</strong></li><li>부가세액 확인<strong>${vat}건</strong></li><li>개인거래·중복 등 제외<strong>${state.summary.excluded_ids.length}건</strong></li></ul><a class="button" href="#transactions">거래 검토하기</a></section></div><section class="panel table-panel"><div class="panel-heading"><div><h2>최근 거래</h2><p>거래를 선택해 분류와 증빙 금액을 확인하세요.</p></div><a href="#transactions" class="text-link">전체 보기</a></div>${transactionsTable(selectedTransactions().slice(0,5),true)}</section>`;
}
function transactionsView() {
  let rows = selectedTransactions();
  if (transactionFilter === "pending") rows = rows.filter(tx => state.summary.pending_ids.includes(tx.id)||state.summary.vat_pending_ids.includes(tx.id));
  else if (["income","expense"].includes(transactionFilter)) rows = rows.filter(tx=>tx.direction===transactionFilter);
  else if (transactionFilter === "excluded") rows = rows.filter(tx=>state.summary.excluded_ids.includes(tx.id));
  if (search) rows = rows.filter(tx => [tx.merchant,tx.source,...tx.allocations.map(p=>p.category||"미분류")].join(" ").toLowerCase().includes(search.toLowerCase()));
  return `${heading("하나씩 확인하는 거래",`${year()}년 ${state.summary.count}건 · 원본을 보존하고 분류를 기록해요.`,`<a class="button" href="#ledger">간편장부 보기</a>${importButton}`)}<div class="toolbar"><div class="tabs">${[["all","전체"],["pending","확인 필요"],["income","입금"],["expense","출금"],["excluded","집계 제외"]].map(([id,label])=>`<button class="tab ${transactionFilter===id?"active":""}" data-filter="${id}">${label}</button>`).join("")}</div><label class="search">${icon("search")}<input id="search" type="text" placeholder="거래처·출처·분류 검색" value="${esc(search)}" aria-label="거래 검색"></label></div><section class="panel table-panel">${transactionsTable(rows)}</section><p class="small section-gap">취소·환불은 원거래와 연결해 주세요. 카드 사용과 계좌 결제가 중복되면 한 건을 원거래에 연결해 제외할 수 있어요.</p>`;
}
const fieldNames = {date:"거래일",merchant:"거래처·내용",direction:"입출금 구분",amount:"거래 총액",vat:"부가세액 · 선택",source:"자료 출처 · 선택",reference:"거래번호 · 선택"};
function importView() {
  return `${heading("흩어진 거래를 한곳에", "CSV의 열을 연결하고, 저장 전에 내용을 확인해요.",'<button class="button" data-action="demo">예시로 둘러보기</button>')}<div class="import-layout"><section class="panel upload-panel"><div class="step-label">파일 선택</div><h2>거래 내역을 가져오세요</h2><p class="small">UTF-8·CP949 CSV · 최대 2MB, 2,000행</p><label class="dropzone"><input type="file" id="csv-file" accept=".csv,text/csv" aria-label="거래 CSV 파일 선택"><span class="upload-symbol">＋</span><strong>${upload ? esc(upload.filename) : "CSV 파일을 선택해 주세요"}</strong><small>${upload ? `${upload.inspection.row_count}개 거래 행 · ${esc(upload.inspection.encoding)}` : "날짜·거래처·입출금·금액 열이 필요해요."}</small></label><label class="field">기본 자료 출처<input id="source-name" type="text" maxlength="100" placeholder="예: 사업용 통장" value="${esc(upload?.source_name || "거래명세서")}"><small>출처 열이 없는 파일에 사용해요. 거래번호는 출처별로 구분합니다.</small></label>${upload ? `<div class="mapping"><div class="step-label">열 연결</div><h2>각 열이 어떤 정보인지 알려주세요</h2><div class="mapping-grid">${Object.entries(fieldNames).map(([field,label])=>`<label class="field">${label}<select data-mapping="${field}"><option value="">${["vat","source","reference"].includes(field)?"연결 안 함":"선택해 주세요"}</option>${upload.inspection.headers.map(h=>`<option value="${esc(h)}" ${upload.mapping[field]===h?"selected":""}>${esc(h)}</option>`).join("")}</select></label>`).join("")}</div><button class="button primary" data-action="preview">미리보기 확인</button></div>` : ""}${preview ? previewHTML() : ""}</section><aside class="panel help-panel"><h3>가져오기 전에</h3><ul><li>금액은 부가세를 포함한 거래 총액으로 연결해 주세요.</li><li>입출금은 ‘입금’ 또는 ‘출금’으로 표시해요.</li><li>취소·환불은 원래 입출금 유형의 음수 금액으로 입력해요.</li></ul><div class="divider"><h3>확인하며 기록해요</h3><p>중복 후보는 직접 확인하고, 오류가 있으면 파일을 수정한 뒤 다시 가져와 주세요.</p><p>부가세가 없으면 확인 필요로 남겨요. 임의로 0원이나 10%로 계산하지 않아요.</p><a class="text-link" href="/demo.csv" download>예시 CSV 내려받기</a></div></aside></div>`;
}
function previewHTML() {
  const labels={new:"새 거래",error:"오류",possible_duplicate:"중복 후보",already_imported:"이미 가져옴"};
  return `<div class="mapping"><div class="step-label">저장 전 확인</div><h2>미리보기</h2><div class="preview-summary"><span class="status purple">전체 ${preview.row_count}행</span><span class="status ${preview.error_count?"red":"green"}">오류 ${preview.error_count}행</span><span class="status amber">중복·기등록 ${preview.duplicate_count}행</span></div><div class="preview-table"><table><thead><tr><th>원본 행</th><th>상태</th><th>거래처·내용</th><th>거래일</th><th class="money">금액</th></tr></thead><tbody>${preview.rows.map(r=>`<tr><td>${r.row_number}</td><td><span class="status ${r.status==="error"?"red":r.status==="new"?"green":"amber"}">${labels[r.status]}</span></td><td class="${r.error?"error-text":""}">${esc(r.error||r.merchant)}</td><td>${esc(r.date||"—")}</td><td class="money">${r.amount_krw ? money(r.amount_krw)+"원" : "—"}</td></tr>`).join("")}</tbody></table></div><div class="duplicate-decisions">${preview.rows.filter(r=>r.status==="possible_duplicate").map(r=>`<label class="duplicate-decision"><span>${r.row_number}행 · ${esc(r.merchant)} · ${money(r.amount_krw)}원<small>같은 내용의 거래가 있습니다.</small></span><select data-decision="${r.row_number}"><option value="">직접 확인해 주세요</option value="skip">중복이므로 제외</option value="keep">별도 거래로 가져오기</option></select></label>`).join("")}</div><div class="import-confirm"><button class="button primary" id="commit-import" data-action="commit" ${preview.error_count||preview.rows.some(r=>r.status==="possible_duplicate")?"disabled":""}>${preview.already_imported?"가져오기 결과 확인":"확인한 거래 저장"}</button><p>${preview.error_count?"오류 행을 수정한 파일로 다시 시도해 주세요.":"저장 후 거래를 분류할 수 있어요. 원본도 함께 보관합니다."}</p></div></div>`;
}
function ledgerView() {
  const s=state.summary, ready=!!s.count&&!s.pending_ids.length&&!s.vat_pending_ids.length&&state.settings.vat_mode!=="unconfirmed";
  const table=s.rows.length?`<div class="table-wrap"><table><thead><tr><th>일자</th><th>거래처</th><th>분류</th><th class="money">장부 금액</th><th class="money">기록 부가세</th><th class="money">총액</th></tr></thead><tbody>${s.rows.map(r=>`<tr><td>${r.date}</td><td>${esc(r.merchant)}</td><td>${esc(r.category)}</td><td class="money">${r.book_amount_krw===null?'<span class="status amber">확인 필요</span>':money(r.book_amount_krw)}</td><td class="money">${!r.vat_separated?'구분 없음':r.book_vat_krw===null?'—':money(r.book_vat_krw)}</td><td class="money">${money(r.gross_krw)}</td></tr>`).join("")}</tbody></table></div>`:'<div class="empty"><p>거래를 분류하면 장부에 표시됩니다.</p><a class="button" href="#transactions">거래 검토하기</a></div>';
  return `${heading("확인한 기록이 장부가 되도록",`${year()}년 · ${vatModes[state.settings.vat_mode]||"과세유형 확인 전"} · 간편장부 정리표(검토용)`,`<a class="button" href="/api/export.csv?kind=transactions&year=${year()}">거래내역 CSV</a><button class="button primary" data-action="export-ledger" ${s.reviewed?"":"disabled"}>장부 CSV 내려받기</button>`)}${reviewNotice()}${metrics()}<section class="panel"><div class="ledger-review"><div><h2>선택 연도의 자료를 확인해 주세요</h2><p class="small">수입은 선택한 과세유형에 따라, 비용은 확인한 증빙 금액으로 기록해요. 공제 가능 여부는 자동 판정하지 않아요.</p><label class="checkbox"><input id="review-confirmed" type="checkbox" ${ready&&!s.reviewed?"":"disabled"}>선택 연도의 자료가 모두 포함되었는지, 분류와 증빙 금액을 확인했습니다.</label></div><button class="button primary" data-action="review" ${ready&&!s.reviewed?"":"disabled"}>자료 확인 완료</button></div></section><section class="panel table-panel section-gap"><div class="panel-heading"><div><h2>수입·비용·자산 기록</h2><p>분류한 거래만 표시해요. 미분류·제외 거래는 거래내역 CSV에 함께 남아요.</p></div><a class="text-link" href="#transactions">거래 수정</a></div>${table}</section><p class="small section-gap">간이·면세사업자의 수입은 총액을 기록합니다. 자료·과세유형을 바꾸면 확인이 다시 필요해요. 유형 전환·겸업·감가상각 등 세무 조정은 별도 검토가 필요합니다.</p>`;
}
function taxView() {
  if (year()!==2025) return `${heading("세금과 근거를 함께", "검증한 귀속연도와 적용 범위를 확인해요.")}<section class="panel empty"><div class="empty-symbol">▤</div><h2>${year()}년 계산 규칙은 아직 준비 중이에요</h2><p>거래 정리와 장부는 계속 사용할 수 있습니다. 현재 검증한 계산요소는 2025년 귀속입니다.</p><button class="button" data-action="year-2025">2025년 계산요소 보기</button></section>`;
  return `${heading("숫자마다 근거가 있는 계산", "2025년 귀속 · 검증한 계산요소를 확인해요.")}<div class="notice"><p><strong>계산 단계마다 범위가 달라요.</strong> 사업소득금액과 과세표준을 구분하며, 공제·감면과 최종 납부세액은 별도 확인이 필요해요.</p></div><div class="tax-grid"><section class="panel"><span class="status purple">기본세율</span><h2 class="section-gap">확정 과세표준으로 계산</h2><p class="tax-intro">소득공제 등을 반영한 과세표준을 직접 입력하세요. 장부 차액을 자동으로 대입하지 않아요.</p><form id="base-tax" class="tax-form"><label class="field">과세표준 (원)<input type="text" name="base" inputmode="numeric" placeholder="예: 30000000" required pattern="[0-9]+"></label><label class="checkbox"><input name="confirmed" type="checkbox" required>공제 등을 반영한 과세표준을 별도로 확인했습니다.</label><button class="button primary">기본 산출세액 계산</button></form><div id="base-result" aria-live="polite"></div></section><section class="panel"><span class="status purple">경비율</span><h2 class="section-gap">추계 사업소득금액 계산</h2><p class="tax-intro">선택한 방법의 적용 자격을 확인한 단일 사업장·업종을 위한 계산요소예요.</p>${!state.rates.available?'<div class="notice warning"><p>검토한 경비율 자료가 준비되지 않았어요. 기본세율 계산은 사용할 수 있습니다.</p></div>':""}<form id="business-tax" class="tax-form"><div class="form-grid"><label class="field">업종코드<input name="industry" type="text" inputmode="numeric" placeholder="예: 940908" required pattern="[0-9]{6}" maxlength="6"></label><label class="field">연간 수입금액 (원)<input name="revenue" type="text" inputmode="numeric" required pattern="[0-9]+" placeholder="예: 45000000"></label><label class="field full">적용할 방법<select name="method" id="tax-method"><option value="simple">단순경비율</option><option value="standard">기준경비율</option></select></label></div><div id="standard-fields" hidden><div class="form-grid"><label class="field full">기장 의무 구분<select name="bookkeeping"><option value="simple">간편장부대상자</option><option value="double">복식부기의무자</option></select></label>${[["purchases","매입비용"],["rent","임차료"],["payroll","인건비"]].map(([name,label])=>`<label class="field">${label} (원)<input name="${name}" type="text" inputmode="numeric" placeholder="확인한 금액"></label>`).join("")}</div><label class="checkbox section-gap"><input name="expenses-confirmed" type="checkbox">주요경비의 증빙·범위·조정을 별도로 확인했습니다.</label></div><div class="tax-checks"><label class="checkbox"><input name="single" type="checkbox" required>단일 사업장·단일 업종이며 공동사업이 아닙니다.</label><label class="checkbox"><input name="eligible" type="checkbox" required>직전·해당 연도 수입 등 선택 방법의 적용 자격을 확인했습니다.</label><label class="checkbox"><input name="general" type="checkbox" required>자가율·장애인 특례 등 조정 없이 일반율을 적용할 수 있습니다.</label><label class="checkbox"><input name="adjustments" type="checkbox" required>일자리안정자금·연말정산 사업소득 등 별도 적용 사항이 없습니다.</label></div><button class="button primary" ${state.rates.available?"":"disabled"}>사업소득금액 계산</button></form><div id="business-result" aria-live="polite"></div></section></div>`;
}
function render() {
  if (!state) return;
  $("#year").innerHTML=state.years.map(y=>`<option value="${y}" ${y===year()?"selected":""}>${y}년</option>`).join("");
  $("#business-name").textContent=state.settings.name;
  $("#nav-count").textContent=state.summary.pending_ids.length||"";
  $("#breadcrumb-view").textContent=viewNames[view];
  document.querySelectorAll("[data-nav]").forEach(a=>{const active=a.dataset.nav===(view==="ledger"?"overview":view);a.classList.toggle("active",active);if(active)a.setAttribute("aria-current","page");else a.removeAttribute("aria-current");});
  $("#main").innerHTML=({overview,transactions:transactionsView,import:importView,ledger:ledgerView,tax:taxView})[view]();
}
function navigate() { if(location.hash==="#main"){$("#main").focus();return;}view=Object.hasOwn(viewNames,location.hash.slice(1))?location.hash.slice(1):"overview"; render();window.scrollTo(0,0); }
function bytesToBase64(bytes) { let text="";for(let i=0;i<bytes.length;i+=32768)text+=String.fromCharCode(...bytes.subarray(i,i+32768));return btoa(text); }
async function selectFile(filename, bytes) {
  if(bytes.length>2*1024*1024)throw new Error("2MB 이하의 CSV를 선택해 주세요.");
  const content=bytesToBase64(bytes), inspection=await api("/api/import/inspect",{content});
  upload={filename,content,inspection,mapping:{...inspection.mapping},source_name:"거래명세서"};preview=null;
  if(view!=="import")location.hash="import";else render();
}
function partsHTML(parts) {
  return parts.map(part=>`<div class="allocation"><div class="allocation-grid"><label class="field">분류<select data-part="category"><option value="">미분류</option>${state.categories[activeTransaction.direction].map(c=>`<option ${part.category===c?"selected":""}>${c}</option>`).join("")}</select></label><label class="field">거래 총액 (원)<input data-part="amount_krw" type="text" inputmode="numeric" value="${esc(part.amount_krw)}" required></label><label class="field">부가세액 (원)<input data-part="vat_krw" type="text" inputmode="numeric" value="${esc(part.vat_krw)}" placeholder="미확인"></label><button class="icon-button" type="button" data-action="remove-part" aria-label="분할 항목 삭제">×</button></div><label class="field note-field">메모<input data-part="note" type="text" maxlength="500" value="${esc(part.note)}" placeholder="증빙·사용 목적 등"></label></div>`).join("");
}
function readParts() { return [...$("#editor").querySelectorAll(".allocation")].map(card=>Object.fromEntries(["category","amount_krw","vat_krw","note"].map(key=>[key,$(`[data-part="${key}"]`,card).value || (key==="category"||key==="vat_krw"?null:"")]))); }
function updateSplitTotal() {
  try { const sum=readParts().reduce((n,p)=>n+BigInt(p.amount_krw.replaceAll(",","")),0n);$("#split-total").textContent=`분할 합계 ${money(sum)}원 / 원래 금액 ${money(activeTransaction.amount_krw)}원`; } catch { $("#split-total").textContent="각 분할 금액을 원 단위로 입력해 주세요."; }
}
function linkOptions() {
  const kind=$("#link-kind").value, selected=$("#link-target").value || activeTransaction.link_target;
  $("#link-target-field").hidden=!kind;
  const options=state.transactions.filter(tx=>tx.id!==activeTransaction.id&&!tx.link_kind&&BigInt(tx.amount_krw)>0n&&tx.direction===activeTransaction.direction&&tx.date.slice(0,4)===activeTransaction.date.slice(0,4)&&(kind!=="duplicate"||tx.amount_krw===activeTransaction.amount_krw));
  $("#link-target").innerHTML='<option value="">원거래를 선택해 주세요</option>'+options.map(tx=>`<option value="${tx.id}" ${selected===tx.id?"selected":""}>${esc(tx.date)} · ${esc(tx.merchant)} · ${money(tx.amount_krw)}원</option>`).join("");
}
async function openEditor(id) {
  activeTransaction=state.transactions.find(tx=>tx.id===id); if(!activeTransaction)return;
  const tx=activeTransaction, dialog=$("#editor");
  dialog.innerHTML=`<form id="edit-transaction"><div class="dialog-head"><div><h2 id="editor-title">거래 검토</h2><p>원본은 보존하고 분류와 연결을 기록해요.</p></div><button class="icon-button" type="button" data-action="close-editor" aria-label="거래 검토 닫기">×</button></div><div class="dialog-body"><div class="original"><div><strong>${esc(tx.merchant)}</strong><small>${esc(tx.date)} · ${esc(tx.source)} · 원본 ${tx.row_number}행</small></div><strong>${money(tx.amount_krw)}원</strong></div><div id="editor-error" class="result-error" role="alert"></div><div class="split-head"><h3>분류와 금액</h3><button class="button quiet compact" type="button" data-action="add-part">＋ 거래 분할</button></div><div id="allocations">${partsHTML(tx.allocations)}</div><p class="split-total" id="split-total"></p><p class="small">부가세를 확인할 수 없으면 비워 두세요. 증빙에서 0원임을 확인했다면 0을 입력해 주세요.</p><div class="link-section"><h3>중복·취소·환불 연결</h3><div class="form-grid"><label class="field">연결 방법<select id="link-kind"><option value="" ${!tx.link_kind?"selected":""}>별도 거래로 기록</option><option value="duplicate" ${tx.link_kind==="duplicate"?"selected":""}>같은 거래의 중복 자료 · 집계 제외</option><option value="refund" ${tx.link_kind==="refund"?"selected":""}>원거래의 취소·환불 · 음수 금액</option></select></label><label class="field" id="link-target-field">원래 거래<select id="link-target"></select></label></div></div><details class="history"><summary>변경 이력 확인</summary><div id="transaction-history">불러오는 중…</div></details></div><div class="dialog-actions"><button class="button" type="button" data-action="close-editor">닫기</button><button class="button primary">변경 저장</button></div></form>`;
  linkOptions();updateSplitTotal();dialog.showModal();
  try { const history=await api(`/api/history/${id}`);if(activeTransaction?.id===id)$("#transaction-history").innerHTML=`<ul>${history.map(item=>`<li>${new Date(item.at).toLocaleString("ko-KR")} · ${item.before?"거래 수정":"CSV에서 가져옴"}<br>${item.after.allocations.map(p=>`${esc(p.category||"미분류")} ${money(p.amount_krw)}원`).join(" / ")}${item.after.link_kind?` · ${item.after.link_kind==="refund"?"환불 연결":"중복 연결"}`:""}</li>`).join("")}</ul>`; } catch(error){$("#transaction-history").textContent=error.message;}
}
function renderResult(container, result, label, amount) {
  $(container).innerHTML=`<div class="result"><div class="result-label">${label}</div><div class="result-value">${esc(decimalMoney(amount))}원</div><p>최종 납부세액은 별도로 확인해 주세요.</p><details><summary>계산 과정과 근거 보기</summary><ol>${result.steps.map(step=>`<li>${esc(stepNames[step.id]||"계산 단계")} · ${esc(decimalMoney(step.result_krw_exact))}원${step.rate_pct?` · 적용률 ${esc(step.rate_pct)}%`:""}</li>`).join("")}</ol>${Object.values(result.sources).map(source=>`<div><a href="${esc(source.url)}" target="_blank" rel="noreferrer">${esc(source.publisher)} · ${esc(source.locator)}</a></div>`).join("")}<small>규칙 ${esc(result.rule_version)}<br>자료·규칙 확인값 ${esc(result.rule_sha256)}</small></details></div>`;
}
document.addEventListener("click", async event => {
  const filter=event.target.closest("[data-filter]"); if(filter){transactionFilter=filter.dataset.filter;render();return;}
  const button=event.target.closest("[data-action]"), row=event.target.closest("[data-transaction]");
  if(!button&&row){await openEditor(row.dataset.transaction);return;} if(!button||button.disabled)return;
  try {
    switch(button.dataset.action) {
      case "demo": {const response=await fetch("/demo.csv");await selectFile("예시 거래내역.csv",new Uint8Array(await response.arrayBuffer()));break;}
      case "preview": {upload.source_name=$("#source-name").value;preview=await api("/api/import/preview",{...upload,mapping:upload.mapping,source_name:upload.source_name});render();break;}
      case "commit": {const decisions=Object.fromEntries([...document.querySelectorAll("[data-decision]")].map(select=>[select.dataset.decision,select.value]));button.disabled=true;const result=await api("/api/import/commit",{token:preview.token,decisions});upload=null;preview=null;await refresh();location.hash="transactions";toast(`${result.imported}건을 가져왔어요. ${result.skipped}건은 중복·기등록 등으로 제외했습니다.`);break;}
      case "edit": await openEditor(button.dataset.id);break;
      case "close-editor": $("#editor").close();break;
      case "add-part": $("#allocations").insertAdjacentHTML("beforeend",partsHTML([{category:null,amount_krw:"0",vat_krw:activeTransaction.vat_krw===null?null:"0",note:""}]));updateSplitTotal();break;
      case "remove-part": if($("#allocations").children.length>1){button.closest(".allocation").remove();updateSplitTotal();}else toast("한 개 이상의 분류 항목이 필요해요.");break;
      case "review": if(!$("#review-confirmed").checked)throw new Error("선택 연도 자료 확인에 체크해 주세요.");await api("/api/review",{year:year(),confirmed:true});await refresh();toast("자료 확인을 기록했어요.");break;
      case "export-ledger": location.href=`/api/export.csv?kind=ledger&year=${year()}`;break;
      case "year-2025": await api("/api/settings",{name:state.settings.name,year:2025});await refresh();break;
      case "close-profile": $("#profile-dialog").close();break;
      case "open-profile": $("#profile").click();break;
    }
  } catch(error) { toast(error.message);if($("#editor").open)$("#editor-error").textContent=error.message;if(button.dataset.action==="commit")button.disabled=false; }
});
document.addEventListener("change", async event => {
  try {
    if(event.target.id==="csv-file"&&event.target.files.length){const file=event.target.files[0];await selectFile(file.name,new Uint8Array(await file.arrayBuffer()));}
    if(event.target.dataset.mapping){upload.mapping[event.target.dataset.mapping]=event.target.value;preview=null;render();}
    if(event.target.dataset.decision)$("#commit-import").disabled=[...document.querySelectorAll("[data-decision]")].some(select=>!select.value);
    if(event.target.id==="year"){await api("/api/settings",{name:state.settings.name,year:Number(event.target.value)});await refresh();}
    if(event.target.id==="link-kind")linkOptions();
    if(event.target.id==="tax-method")$("#standard-fields").hidden=event.target.value!=="standard";
  } catch(error){toast(error.message);}
});
document.addEventListener("input",event=>{
  if(event.target.id==="search"){search=event.target.value;const start=event.target.selectionStart;render();$("#search").focus();$("#search").setSelectionRange(start,start);}
  if(event.target.dataset.part==="amount_krw")updateSplitTotal();
  if(event.target.id==="source-name"&&upload){upload.source_name=event.target.value;if(preview){preview=null;const confirm=$("#commit-import");if(confirm)confirm.disabled=true;}}
});
document.addEventListener("submit",async event=>{
  if(!["edit-transaction","profile-form","base-tax","business-tax"].includes(event.target.id))return;
  event.preventDefault();const form=event.target, data=new FormData(form), submit=form.querySelector('button:not([type="button"])');if(submit)submit.disabled=true;
  try {
    if(form.id==="edit-transaction") {await api(`/api/transaction/${activeTransaction.id}`,{revision:activeTransaction.revision,allocations:readParts(),link_kind:$("#link-kind").value,link_target:$("#link-kind").value?$("#link-target").value:null});$("#editor").close();await refresh();toast("분류와 변경 이력을 저장했어요.");}
    else if(form.id==="profile-form"){await api("/api/settings",{name:data.get("name"),year:year(),vat_mode:data.get("vat-mode")});$("#profile-dialog").close();await refresh();}
    else if(form.id==="base-tax"){const result=await api("/api/tax/base",{year:year(),taxable_income_krw:data.get("base"),confirmed:data.get("confirmed")==="on"});renderResult("#base-result",result,"공제·감면 적용 전 기본 산출세액",result.calculated_tax_krw_exact);}
    else {const calculation={tax_year:year(),method:data.get("method"),industry_code:data.get("industry"),revenue_krw:data.get("revenue"),conditions:{single_business:data.get("single")==="on",single_industry:data.get("single")==="on",joint_business:false,eligibility_confirmed:data.get("eligible")==="on",general_rate_applicable:data.get("general")==="on",special_adjustments:data.get("adjustments")==="on"?[]:["unconfirmed"],job_stabilization_funds_krw:"0"}};if(calculation.method==="standard")Object.assign(calculation,{bookkeeping:data.get("bookkeeping"),major_expenses_confirmed:data.get("expenses-confirmed")==="on",major_expenses:{purchases_krw:data.get("purchases"),rent_krw:data.get("rent"),payroll_krw:data.get("payroll")}});const result=await api("/api/tax/business",{calculation,version_id:state.rates.version_id});renderResult("#business-result",result,"공제 전 사업소득금액",result.business_income_krw_exact);}
  }catch(error){toast(error.message);if(form.id==="edit-transaction")$("#editor-error").textContent=error.message;}finally{if(submit&&submit.isConnected)submit.disabled=false;}
});
$("#profile").addEventListener("click",()=>{const dialog=$("#profile-dialog");dialog.innerHTML=`<form id="profile-form"><div class="dialog-head"><h2 id="profile-title">사업장 설정</h2><button type="button" class="icon-button" data-action="close-profile" aria-label="사업장 설정 닫기">×</button></div><div class="dialog-body"><div class="form-grid"><label class="field full">장부에 표시할 이름<input name="name" type="text" maxlength="80" value="${esc(state.settings.name)}" required></label><label class="field full">${year()}년 과세유형<select name="vat-mode">${Object.entries(vatModes).map(([value,label])=>`<option value="${value}" ${state.settings.vat_mode===value?"selected":""}>${label}</option>`).join("")}</select><small>사업자등록 정보 등을 확인한 뒤 선택해 주세요. 연도 중 유형 전환·겸업은 별도 검토가 필요해요.</small></label></div></div><div class="dialog-actions"><button class="button primary">저장</button></div></form>`;dialog.showModal();});
window.addEventListener("hashchange",navigate);
refresh().then(navigate).catch(error=>{$("#main").innerHTML=`<section class="panel empty"><h1>장부를 불러오지 못했어요</h1><p>${esc(error.message)}</p><button class="button" data-action="reload">새로고침</button></section>`;document.querySelector('[data-action="reload"]').addEventListener("click",()=>location.reload());});
