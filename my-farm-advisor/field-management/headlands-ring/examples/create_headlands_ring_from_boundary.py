#!/usr/bin/env python3
"""Example: Create a headlands ring from a field boundary file.

Usage:
    python create_headlands_ring_from_boundary.py --input <path_to_boundary.geojson>
    python create_headlands_ring_from_boundary.py --input ../field-boundaries/examples/real_10_fields_iowa.geojson --width 21.0

Output:
    field_boundary.gpkg    - Field boundary with meters_squared and acres
    headlands_ring.gpkg    - Headlands ring with meters_squared and acres
"""

import argparse
import sys
from pathlib import Path

import geopandas as gpd

# Allow importing headlands_ring when running from examples/ directly
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from headlands_ring import create_headlands_ring_from_boundary


def main():
    parser = argparse.ArgumentParser(
        description="Create headlands ring from a field boundary in EPSG:4326"
    )
    parser.add_argument(
        "--input",
        required=True,
        help="Path to input boundary file (GeoJSON, Shapefile, GeoPackage, etc.)",
    )
    parser.add_argument(
        "--width",
        type=float,
        default=21.0,
        help="Inward buffer width in meters (default: 21.0)",
    )
    args = parser.parse_args()

    print(f"Loading boundary from: {args.input}")
    boundary_gdf = gpd.read_file(args.input)
    print(f"Loaded {len(boundary_gdf)} feature(s), CRS: {boundary_gdf.crs}")

    original_gdf, headlands_gdf = create_headlands_ring_from_boundary(
        boundary_gdf, width_m=args.width
    )

    print("\n--- Results ---")
    print(
        f"Field area:   {original_gdf['acres'].iloc[0]:.2f} acres "
        f"({original_gdf['meters_squared'].iloc[0]:,.0f} m²)"
    )
    if not headlands_gdf.empty:
        print(
            f"Headlands:    {headlands_gdf['acres'].iloc[0]:.2f} acres "
            f"({headlands_gdf['meters_squared'].iloc[0]:,.0f} m²)"
        )
    else:
        print("Headlands:    empty (buffer consumed entire field)")
    print("\nOutput files:")
    print("  - field_boundary.gpkg")
    print("  - headlands_ring.gpkg")


if __name__ == "__main__":
    main()
