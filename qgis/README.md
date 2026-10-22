# QGIS 작업본

`build_qgis_package.py`는 분석 산출물을 하나의 GeoPackage로 묶고 기본 QGIS 스타일을
GeoPackage 내부 `layer_styles` 테이블에 저장합니다.

```powershell
.\.venv\Scripts\python.exe main.py --config config.yaml
.\.venv\Scripts\python.exe build_qgis_package.py
```

QGIS에서 `output/qgis/sujeong5_fire_safety.gpkg`를 열고 다음 레이어를 추가하면 됩니다.

- `candidates`: 후보 10곳과 우선 3곳
- `existing_fire_infrastructure`: 비상소화장치·소방용수
- `boundary`: 수정5동 행정동 경계
- `buildings`: 공식 GIS건물통합정보
- `official_roads`: 공식 도로명주소 도로와 등록폭
- `walk_roads`: 분석에 사용한 OSM 보행망

QGIS 실행 파일이 현재 PC에 없어 화면 렌더링은 검수하지 못했습니다. GeoPackage의 레이어
개수, 좌표계, 공간 범위와 스타일 레코드는 스크립트로 검증합니다. QGIS 3.44 문서 기준으로
QML은 레이어 스타일을 보존하는 형식이며 GeoPackage 내부 스타일 저장도 지원됩니다.
