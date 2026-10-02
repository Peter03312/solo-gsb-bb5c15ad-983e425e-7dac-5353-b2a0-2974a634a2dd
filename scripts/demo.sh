#!/usr/bin/env bash
# 端到端演示：登记基准 -> 逐级签发 -> 非法引用/时间重叠/并发报错 -> 撤销级联。
# 用法：scripts/demo.sh [BASE_URL]
set -u
BASE="${1:-http://localhost:8080}"
CT="Content-Type: application/json"

say()  { printf '\n========== %s ==========\n' "$1"; }
post() { # post <路径> <JSON>  （始终打印 HTTP 状态码与响应体）
  curl -sS -w '\n[HTTP %{http_code}]\n' -X POST "$BASE$1" -H "$CT" -d "$2"
}
get()  { curl -sS -w '\n[HTTP %{http_code}]\n' "$BASE$1"; }

say "1. 登记设备（标准器与被校设备）"
post /devices '{"code":"STD-01","name":"一等标准压力计"}'
post /devices '{"code":"DUT-11","name":"现场压力表 11 号"}'
post /devices '{"code":"DUT-12","name":"过程校验仪 12 号"}'
post /devices '{"code":"DUT-RACE","name":"并发测试专用表计"}'

say "2. 登记基准设备合格证书（追溯起点）"
post /certificates/reference '{
  "cert_no":"REF-2026-001",
  "device_code":"STD-01",
  "calibrated_at":"2026-01-10T08:00:00+00:00",
  "valid_from":"2026-01-10T00:00:00+00:00",
  "valid_until":"2027-01-10T00:00:00+00:00",
  "conclusion":"PASS"
}'

say "3. 签发普通证书 C-002：校准时刻落在基准证书有效期内 -> 成功"
post /certificates '{
  "cert_no":"C-2026-002",
  "device_code":"DUT-11",
  "calibrated_at":"2026-09-01T08:00:00+00:00",
  "valid_from":"2026-09-01T00:00:00+00:00",
  "valid_until":"2027-09-01T00:00:00+00:00",
  "conclusion":"PASS",
  "parent_cert_no":"REF-2026-001"
}'

say "4. 再签一级 C-003，引用 C-002 -> 成功（链：C-003 -> C-002 -> REF-001）"
post /certificates '{
  "cert_no":"C-2026-003",
  "device_code":"DUT-12",
  "calibrated_at":"2026-09-20T08:00:00+00:00",
  "valid_from":"2026-09-20T00:00:00+00:00",
  "valid_until":"2027-09-20T00:00:00+00:00",
  "conclusion":"PASS",
  "parent_cert_no":"C-2026-002"
}'

say "5. 报错示例：校准时刻早于/晚于上游有效期 -> 409 PARENT_NOT_QUALIFIED"
post /certificates '{
  "cert_no":"C-BAD-1",
  "device_code":"DUT-11",
  "calibrated_at":"2027-02-01T08:00:00+00:00",
  "valid_from":"2027-02-01T00:00:00+00:00",
  "valid_until":"2028-02-01T00:00:00+00:00",
  "conclusion":"PASS",
  "parent_cert_no":"REF-2026-001"
}'

say "6. 报错示例：同设备生效证书有效期重叠 -> 409 VALIDITY_OVERLAP"
post /certificates '{
  "cert_no":"C-BAD-2",
  "device_code":"DUT-11",
  "calibrated_at":"2026-10-01T08:00:00+00:00",
  "valid_from":"2026-10-01T00:00:00+00:00",
  "valid_until":"2027-03-01T00:00:00+00:00",
  "conclusion":"PASS",
  "parent_cert_no":"REF-2026-001"
}'

say "7. 报错示例：证书编号重复签发 -> 409 DUPLICATE_CERT_NO"
post /certificates '{
  "cert_no":"C-2026-002",
  "device_code":"DUT-12",
  "calibrated_at":"2026-09-21T08:00:00+00:00",
  "valid_from":"2026-09-21T00:00:00+00:00",
  "valid_until":"2027-09-21T00:00:00+00:00",
  "conclusion":"PASS",
  "parent_cert_no":"REF-2026-001"
}'

say "8. 报错示例：时间范围非法（valid_from >= valid_until）-> 422 TIME_RANGE_INVALID"
post /certificates '{
  "cert_no":"C-BAD-3",
  "device_code":"DUT-12",
  "calibrated_at":"2026-09-21T08:00:00+00:00",
  "valid_from":"2027-09-21T00:00:00+00:00",
  "valid_until":"2026-09-21T00:00:00+00:00",
  "conclusion":"PASS",
  "parent_cert_no":"REF-2026-001"
}'

say "9. 并发重复签发：同设备两段重叠有效期同时 POST，恰好一个成功另一个 409"
# 两张新证书窗口高度重叠，且校准时刻均在基准证书有效期内；
# 排他约束保证并发下只有一张落库，另一张得到 CONCURRENT_ISSUANCE / VALIDITY_OVERLAP。
payload1='{"cert_no":"C-RACE-1","device_code":"DUT-RACE","calibrated_at":"2026-05-01T08:00:00+00:00","valid_from":"2026-05-01T00:00:00+00:00","valid_until":"2026-11-01T00:00:00+00:00","conclusion":"PASS","parent_cert_no":"REF-2026-001"}'
payload2='{"cert_no":"C-RACE-2","device_code":"DUT-RACE","calibrated_at":"2026-06-01T08:00:00+00:00","valid_from":"2026-06-01T00:00:00+00:00","valid_until":"2026-12-01T00:00:00+00:00","conclusion":"PASS","parent_cert_no":"REF-2026-001"}'
curl -sS -o /tmp/race1.json -w 'race1 [HTTP %{http_code}]\n' -X POST "$BASE/certificates" -H "$CT" -d "$payload1" &
curl -sS -o /tmp/race2.json -w 'race2 [HTTP %{http_code}]\n' -X POST "$BASE/certificates" -H "$CT" -d "$payload2" &
wait

say "10. 追溯链查询 C-2026-003（自下而上直到基准）"
get /certificates/C-2026-003/traceability

say "11. 查询基准证书的全部下游（撤销前预演受影响范围）"
get /certificates/REF-2026-001/downstream

say "12. 撤销基准证书 -> C-002、C-003 全部级联失效，返回受影响证书与链"
post /certificates/REF-2026-001/revoke '{"reason":"基准器超差召回，证书作废"}'

say "13. 失效后历史内容仍可查（快照保留，status=INVALIDATED）"
get /certificates/C-2026-003
get /certificates/C-2026-003/events

say "14. 报错示例：引用已失效证书再签发 -> 409 PARENT_NOT_QUALIFIED"
post /certificates '{
  "cert_no":"C-AFTER-REVOKE",
  "device_code":"DUT-11",
  "calibrated_at":"2026-11-01T08:00:00+00:00",
  "valid_from":"2026-11-01T00:00:00+00:00",
  "valid_until":"2027-11-01T00:00:00+00:00",
  "conclusion":"PASS",
  "parent_cert_no":"C-2026-002"
}'

say "15. 报错示例：重复撤销 -> 409 CERTIFICATE_NOT_ACTIVE"
post /certificates/REF-2026-001/revoke '{"reason":"再次撤销"}'

printf '\n演示完成。交互式文档：%s/docs\n' "$BASE"
