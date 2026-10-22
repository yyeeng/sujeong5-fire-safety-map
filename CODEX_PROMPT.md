# Codex 작업 지시: 부산 동구 수정5동 보이는 소화기 후보지 지도 분석

당신은 파이썬 GIS 데이터 엔지니어입니다. 이 폴더의 README.md, config.yaml, interview_evidence.md, data_sources.csv를 읽은 뒤 **실제로 실행 가능한 프로그램**을 만들어 주세요. 목적은 기존 소화기 위치자료 없이 '부산광역시 동구 수정제5동'의 보이는 소화기 **설치 우선검토 후보지** 10곳과 추천 3곳을 지도에 표시하는 것입니다. 기존 소화기 부재, 현장 실측 진입불가, 설치 확정 등 검증되지 않은 말을 하지 마세요.

## 개발 환경/산출물
- Python 3.11+, osmnx(2.x), geopandas, shapely, pyproj, networkx, folium, pandas, pyyaml.
- `python -m pip install -r requirements.txt`로 설치 후 `python main.py --config config.yaml`로 실행 가능하게 구현.
- `.env.example`에는 키 이름만: `SGIS_CONSUMER_KEY=`, `SGIS_CONSUMER_SECRET=`. API 키를 소스에 넣거나 공개하지 말 것.
- `input/` 수동 SHP/GeoJSON 파일(있을 경우)을 자동 탐색하고, `cache/`에 API 원본을 보존.
- `output/candidates.csv`, `output/candidates.geojson`, `output/map.html`, `output/roads.geojson`, `output/report.md`, `output/data_quality.json` 생성.
- 테스트 가능한 순수 함수로 스코어 계산 및 단위 테스트 3개 이상 작성.

## 1. 경계 획득
우선순위: (a) input/의 행안부 '구역의 도형'(부산, TL_SCCO_GEMD) → (b) SGIS 행정동 경계 API → (c) 참고용 GitHub https://github.com/schwa456/hangjeongdong 의 ver20241001 → (d) 실패 시 사용자에게 정확한 파일이 필요하다고 오류 메시지. 옛 2024 경계를 쓸 때는 반드시 outdated 경고를 출력하세요. 좌표와 명칭이 '부산 동구 수정제5동/수정5동'에 정확히 대응하는지 검증; 수정동(법정동) 전체를 수정5동이라 오인하지 말 것. SGIS API는 다음 문서에 나온 방식대로 서비스 ID/secret으로 토큰을 받고, `year=2025`, `low_search=0`, 정확한 8자리 행정동 코드 `adm_cd`를 API 조회로 탐색하고 응답을 검사하세요. 코드를 추측하거나 하드코딩하지 마세요. 경계는 메타데이터(원천·기준연도·원본 CRS·면적)와 함께 저장.
SGIS 인증: https://sgisapi.mods.go.kr/OpenAPI3/auth/authentication.json
SGIS 경계: https://sgisapi.mods.go.kr/OpenAPI3/boundary/hadmarea.geojson
**주의** SGIS GeoJSON의 `geometry.coordinates`가 UTM-K 수치로 제공될 가능성이 있으니 숫자 범위를 확인하고 EPSG:5179 → EPSG:4326을 명시적으로 수행. 경계 유효성/도시면적 sanity-check.

## 2. 도로·건물 확보
- 실제 수정5동 polygon 범위에서 OSMnx/Overpass로 보행 가능 경로(walk), 차량용 경로(drive), `highway=steps`, `footway`, `path`, 가능한 경우 width/motor_vehicle/access 태그 및 OSM 건물(footprints)을 수집. `ox.graph.graph_from_polygon`, `ox.features.features_from_polygon` API 문서 참조. 분석은 소규모 영역 대상으로 순차 요청하고 요청 제한, User-Agent, 캐시, 재시도 존중.
- OSM 보행 경로에만 등장하는 길은 '차량진입 제약 **가능성**'으로만 표시하며, 소방차 진입불가 판정으로 표시하지 않기.
- `input/`에 행정안전부 '도로명이 부여된 도로 도형' 데이터가 있으면 `ROAD_BT` (도로폭, m) 속성을 이용해 OSM 도로와 공간 조인. 결측·음수·0 또는 40m 초과 등 이상치를 검출/표시하고, 잘못된 값은 사용하지 말 것. 도로명 부여된 도로 외의 소골목 누락 가능성을 명시할 것. ROAD_BT는 통행차량·주차 후 실제 유효폭과 구분.
- `input/`에 국토교통부 GIS건물통합정보가 있으면 이를 우선 사용해 50m 내 주거용 건물 개수를 셈. 없으면 OSM 건물로 대체하되 building=yes는 주거건물로 간주하지 않고 별도 '용도미상' 구분. 건물 data coverage(미상 비중/건물 개수) 보고.
- 모든 공간 거리 계산은 EPSG:5179(미터 단위)로 투영, 최종 지도 출력은 EPSG:4326.

## 3. 후보지 생성
- 보행 골목의 교차점(3방향 이상), 좁은 구간과 연결된 분기점, 계단의 상·하단 연결점, 주택 밀집 골목 안쪽에 있는 접근 가능한 후보 **지점/구역**을 10개 이상 생성. 75m 이상 공간 분산(중복 방지), 동 경계 내 위치 확인.
- 학교/철도/건물 내부, 고속도로 가운데 또는 도로 밖의 비현실적 후보는 제외. 실제 벽면·설치 권한은 데이터에서 검증할 수 없으므로 '후보 구역 중심점'으로만 제공.

## 4. 연구팀 인터뷰 기반 가중합(WSM) 1개 기법만 사용
A: 소방차 접근 어려움 50%, B: 주거용 건물 밀집 30%, C: 가시성/접근성 20%. 1~5점 공통척도.
- A: `highway=steps`나 도로폭이 좁음(PNU-ANCHOR 기본값 4.0m 미만, 3.5m·6.0m 민감도 참고), 차량 네트워크 단절 등 **증거와 함께** 점수 부여. 4m는 협소도로 노출을 선별하는 분석 기준이지 법적·공식 소방차 진입불가 기준이 아니다. 각 조건과 점수의 명시적 규칙을 코드/보고서에 공개. 도로폭 미상은 0점/5점으로 억지 대입하지 말고 결측으로 표시.
- B: 각 후보지 50m buffer 안 주거 건물 수를 구하고, 후보지 전체의 분위수 기준으로 1~5점 변환(동률·모두 0건 방어 처리). 단순 건물 개수를 노후배선 위험도라고 단정하지 말 것.
- C: 실제 시야는 지도만으로 알 수 없음. 우선 보행골목 3거리·4거리 등 여러 방향에서 접근할 수 있는 **지도 기반 가시성/접근성 대리지표**를 1~5점으로 정의하여 계산하고 `visibility_proxy=true`, `needs_field_check=true`로 표시. 현장/로드뷰 조사로 1~5점 입력 시 이 C값을 대체할 수 있는 CSV 입력 기능 제공.
- `score_map = 0.5*A + 0.3*B + 0.2*C_proxy`이며, 출력에는 '**지도 기반 잠정 적합도**'라고 표기. 확정점수라고 표기하지 말 것. `confidence`, 결측 항목, 근거 tag/원본도 함께 내보낼 것.
- 50:30:20은 인터뷰에서 도출한 연구팀의 주관적 가중치이지 통계적으로 추정된 값이 아니라고 명시.
- A·B·C가 모두 결측/불충분하면 점수를 계산하지 않고 '데이터 부족'으로 표시. 비어있는 자료를 그럴듯한 수치로 메우지 말 것.

## 5. 시각화/제안서
- Folium HTML 지도: 행정동 경계, 골목/계단/도로폭 관측값, 후보지 상위 10곳 마커, Top3 강조. 클릭하면 주소(확인 가능할 경우), 위경도, A/B/C 근거와 종합점수, 확인 필요 사항 표시.
- `output/report.md`에는 데이터의 실제 수집 성공 여부와 행정경계 기준연도, 각 데이터의 개수/누락률, 선정 기준, 가중치, 3개 최상위 지점 선정 이유, 용어 '소방차 진입 제한 가능성', 현장점검 체크리스트를 정리.
- OSM 라이선스 표기(`© OpenStreetMap contributors`)와 공용 타일 정책 준수. 타일을 대량 다운로드하지 말 것.
- 외부 API 연결 실패 시 **데모 지점/임의 점수로 실제 수정5동 추천 결과를 조작하지 말 것**. 테스트 데이터는 `tests/fixtures/`로 격리하고 report에는 수집 실패 원인을 남길 것.
- 마무리 때 어떤 링크가 실제로 열렸는지, API 호출에 성공했는지, 사람이 다운로드해야 할 입력 파일이 무엇인지, 실행 커맨드와 정확한 산출 경로를 요약.
