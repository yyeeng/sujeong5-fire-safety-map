# 우리동네 화재안전 지도

부산 동구 수정제5동의 보행망·계단·도로폭·주거건물을 결합해 보이는 소화기
설치 검토 지점 10곳을 찾는 GIS 프로젝트입니다. 주민 인터뷰를 근거로
소방차 접근 곤란 50%, 주거밀집 30%, 가시성 대리점수 20%를 적용합니다.

> 지도 좌표는 설치 확정지가 아닌 현장조사 전 검토 중심점입니다.

![우리동네 화재안전 지도](output/previews/busan-dashboard.png)

[보이는 소화기 위치 화면](output/previews/busan-locations.png) ·
[분석 보고서](output/report.md) ·
[후보 목록](output/candidates.csv)

## 빠른 시작

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m fire_safety_map.download --config config/project.yaml
.\.venv\Scripts\python.exe -m fire_safety_map.dashboard --config config/project.yaml
.\.venv\Scripts\python.exe -m fire_safety_map.qgis
.\.venv\Scripts\python.exe -m pytest
```

이미 생성된 주민용 지도는 [output/map.html](output/map.html)을 브라우저로 열면 됩니다.
외부 지도 타일을 사용하지 않으므로 OpenStreetMap 타일 서버의 403 응답과 무관하게
건물·도로·후보 레이어를 표시합니다.

## 저장소 구조

```text
assets/                 대시보드 사용법·행동요령 이미지
config/project.yaml     가중치, 임계값, 데이터 출처 설정
docs/                   인터뷰 근거, 데이터 출처, QGIS 안내
fire_safety_map/        다운로드·분석·지도·QGIS Python 패키지
output/                 선별된 최종 지도, 후보, 검증 결과
tests/                  점수 계산과 공식자료 처리 테스트
pyproject.toml          의존성, 실행 명령, pytest 설정
```

원본 ZIP, `input/`, `cache/`, `reference/`, `.env`, 가상환경과 대용량 중간
GeoJSON은 Git에서 제외합니다. 최종 대시보드, 후보 목록, 품질검증 결과와 QGIS
패키지만 버전 관리합니다.

## 주요 명령

설치 후 다음 명령도 사용할 수 있습니다.

|명령|기능|
|---|---|
|`fire-safety-download`|공개 API와 제공 원자료를 검증하고 캐시 생성|
|`fire-safety-build`|가중합 평가, 후보 선정, 주민용 지도와 보고서 생성|
|`fire-safety-qgis`|분석 레이어를 스타일이 포함된 GeoPackage로 묶기|

신규 Overpass 연결이 실패했을 때 검증된 기존 캐시만 재사용하려면 다음과 같이
명시적으로 실행합니다.

```powershell
.\.venv\Scripts\python.exe -m fire_safety_map.download `
  --config config/project.yaml --reuse-osm-cache
```

## 데이터와 해석 범위

- 부산 전역 우선검토 근거는 [PNU-ANCHOR](https://github.com/PPPP98/PNU-ANCHOR)의
  화재·출동 분석을 참조했습니다.
- 행정동 경계, 도로명주소 도로폭, GIS건물통합정보, OpenStreetMap 보행망과
  부산 소방용수 현황을 사용합니다. 상세 출처는 [docs/data_sources.csv](docs/data_sources.csv)에 있습니다.
- 인터뷰 문항과 가중치 연결 근거는 [docs/interviews.md](docs/interviews.md)에 있습니다.
- 도로폭 4m 미만은 협소도로 노출을 비교하기 위한 분석 기준이며 공식적인
  소방차 진입불가 판정이 아닙니다.
- 실제 유효 도로폭, 주정차, 소화기 가시성, 기존 시설, 설치 권한과 관리 주체는
  현장에서 확인해야 합니다.

## 최종 산출물

- `output/map.html`: 빨간 보이는 소화기와 하늘색 소방용수 아이콘을 표시한 지도
- `output/candidates.csv`, `output/candidates.geojson`: 후보 점수와 근거
- `output/report.md`: 방법, 데이터 품질, 한계와 현장 체크리스트
- `output/data_quality.json`: 누락률과 공간 검증 결과
- `output/road_width_sensitivity.csv`: 3.5m·4m·6m 기준 민감도
- `output/qgis/sujeong5_fire_safety.gpkg`: QGIS용 6개 레이어

테스트는 `python -m pytest`로 실행합니다. 분석 자료가 부족하면 임의 결과를 만들지
않고 실패 원인과 누락 항목을 `output/report.md`와 `output/data_quality.json`에 기록합니다.
