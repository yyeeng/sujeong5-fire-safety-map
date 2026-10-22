"""Build a single GeoPackage that opens directly in QGIS with default styles."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import geopandas as gpd


def single_symbol_qml(geometry: str, color: str, outline: str, width: float) -> str:
    symbol_layer = "SimpleMarker" if geometry == "Point" else (
        "SimpleLine" if geometry == "Line" else "SimpleFill"
    )
    props = (
        f'<Option type="Map"><Option name="color" value="{color}" type="QString"/>'
        f'<Option name="outline_color" value="{outline}" type="QString"/>'
        f'<Option name="outline_width" value="{width}" type="QString"/>'
        f'<Option name="line_color" value="{outline}" type="QString"/>'
        f'<Option name="line_width" value="{width}" type="QString"/>'
        '<Option name="size" value="3.6" type="QString"/></Option>'
    )
    return f'''<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis.dtd' 'SYSTEM'>
<qgis version="3.44.0-Solothurn" styleCategories="Symbology">
  <renderer-v2 type="singleSymbol" symbollevels="0">
    <symbols><symbol name="0" type="{geometry.lower()}">
      <layer class="{symbol_layer}" enabled="1" pass="0">{props}</layer>
    </symbol></symbols>
  </renderer-v2>
</qgis>'''


def categorized_qml(field: str, geometry: str, categories: list[tuple[str, str, str]]) -> str:
    category_xml: list[str] = []
    symbol_xml: list[str] = []
    for index, (value, label, color) in enumerate(categories):
        category_xml.append(
            f'<category value="{value}" label="{label}" symbol="{index}" render="true"/>'
        )
        if geometry == "Point":
            layer = (
                '<layer class="SimpleMarker" enabled="1" pass="0"><Option type="Map">'
                f'<Option name="color" value="{color}" type="QString"/>'
                '<Option name="outline_color" value="255,255,255,255" type="QString"/>'
                '<Option name="outline_width" value="0.6" type="QString"/>'
                '<Option name="size" value="4.2" type="QString"/></Option></layer>'
            )
        else:
            layer = (
                '<layer class="SimpleLine" enabled="1" pass="0"><Option type="Map">'
                f'<Option name="line_color" value="{color}" type="QString"/>'
                '<Option name="line_width" value="0.8" type="QString"/></Option></layer>'
            )
        symbol_xml.append(f'<symbol name="{index}" type="{geometry.lower()}">{layer}</symbol>')
    return f'''<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis.dtd' 'SYSTEM'>
<qgis version="3.44.0-Solothurn" styleCategories="Symbology">
  <renderer-v2 type="categorizedSymbol" attr="{field}" symbollevels="0">
    <categories>{''.join(category_xml)}</categories>
    <symbols>{''.join(symbol_xml)}</symbols>
  </renderer-v2>
</qgis>'''


def rule_qml() -> str:
    rules = [
        ("1", '"rank" &lt;= 3', "우선 1–3", "195,66,56,255"),
        ("2", '"rank" &gt; 3 AND "rank" &lt;= 5', "4–5", "182,116,34,255"),
        ("3", '"rank" &gt; 5', "6–10", "52,112,138,255"),
    ]
    rule_xml = "".join(
        f'<rule key="{key}" filter="{expression}" label="{label}" symbol="{index}"/>'
        for index, (key, expression, label, _color) in enumerate(rules)
    )
    symbols = "".join(
        '<symbol name="{index}" type="marker"><layer class="SimpleMarker" enabled="1" pass="0">'
        '<Option type="Map"><Option name="color" value="{color}" type="QString"/>'
        '<Option name="outline_color" value="255,255,255,255" type="QString"/>'
        '<Option name="outline_width" value="0.7" type="QString"/>'
        '<Option name="size" value="5" type="QString"/></Option></layer></symbol>'.format(
            index=index, color=color
        )
        for index, (_key, _expression, _label, color) in enumerate(rules)
    )
    return f'''<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis.dtd' 'SYSTEM'>
<qgis version="3.44.0-Solothurn" styleCategories="Symbology">
  <renderer-v2 type="RuleRenderer" symbollevels="0"><rules>{rule_xml}</rules><symbols>{symbols}</symbols></renderer-v2>
</qgis>'''


def install_styles(package_path: Path, styles: dict[str, str]) -> None:
    with sqlite3.connect(package_path) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS layer_styles (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              f_table_catalog TEXT,
              f_table_schema TEXT,
              f_table_name TEXT,
              f_geometry_column TEXT,
              styleName TEXT,
              styleQML TEXT,
              styleSLD TEXT,
              useAsDefault BOOLEAN,
              description TEXT,
              owner TEXT,
              ui TEXT,
              update_time DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute("DELETE FROM layer_styles")
        for layer_name, qml in styles.items():
            connection.execute(
                """
                INSERT INTO layer_styles (
                  f_table_catalog, f_table_schema, f_table_name,
                  f_geometry_column, styleName, styleQML, styleSLD,
                  useAsDefault, description, owner, ui
                ) VALUES ('', '', ?, 'geom', 'default', ?, '', 1, ?, '', '')
                """,
                (layer_name, qml, "수정5동 보이는 소화기 기본 스타일"),
            )
        connection.commit()


def build(root: Path) -> Path:
    output_dir = root / "output" / "qgis"
    output_dir.mkdir(parents=True, exist_ok=True)
    package_path = output_dir / "sujeong5_fire_safety.gpkg"
    if package_path.exists():
        package_path.unlink()

    inputs = [
        ("boundary", root / "cache" / "boundary.geojson"),
        ("buildings", root / "cache" / "buildings_official.geojson"),
        ("official_roads", root / "output" / "official_roads.geojson"),
        ("walk_roads", root / "output" / "roads.geojson"),
        ("candidates", root / "output" / "candidates.geojson"),
        ("existing_fire_infrastructure", root / "output" / "existing_fire_infrastructure.geojson"),
    ]
    missing = [str(path) for _name, path in inputs if not path.exists()]
    if missing:
        raise FileNotFoundError("Missing generated layers: " + ", ".join(missing))
    for index, (layer_name, path) in enumerate(inputs):
        frame = gpd.read_file(path)
        frame.to_file(
            package_path,
            layer=layer_name,
            driver="GPKG",
            mode="w" if index == 0 else "a",
        )

    styles = {
        "boundary": single_symbol_qml("Polygon", "247,241,230,45", "40,85,150,255", 1.1),
        "buildings": single_symbol_qml("Polygon", "224,216,205,220", "166,160,151,255", 0.25),
        "official_roads": single_symbol_qml("Line", "255,255,255,255", "181,177,169,255", 0.8),
        "walk_roads": single_symbol_qml("Line", "120,130,126,255", "120,130,126,255", 0.45),
        "candidates": rule_qml(),
        "existing_fire_infrastructure": categorized_qml(
            "asset_type",
            "Point",
            [
                ("emergency_fire_equipment", "비상소화장치", "125,45,42,255"),
                ("fire_water", "소방용수", "42,111,120,255"),
            ],
        ),
    }
    install_styles(package_path, styles)
    return package_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    print(build(args.root.resolve()))


if __name__ == "__main__":
    main()
