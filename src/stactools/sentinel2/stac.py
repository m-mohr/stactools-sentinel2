import logging
import os
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Pattern, Final, Any
from datetime import datetime
from stactools.core.projection import reproject_geom
from shapely.geometry import shape as make_shape, mapping
from itertools import chain
import pystac
from pystac.extensions.eo import EOExtension
from pystac.extensions.projection import ProjectionExtension
from pystac.extensions.sat import OrbitState, SatExtension
from pystac.extensions.view import ViewExtension
from stactools.sentinel2.mgrs import MgrsExtension
from stactools.sentinel2.grid import GridExtension

from stactools.core.io import ReadHrefModifier
from stactools.core.projection import transform_from_bbox
from stactools.sentinel2.safe_manifest import SafeManifest
from stactools.sentinel2.product_metadata import ProductMetadata
from stactools.sentinel2.granule_metadata import GranuleMetadata
from stactools.sentinel2.tileinfo_metadata import TileInfoMetadata
from stactools.sentinel2.utils import extract_gsd
from stactools.sentinel2.constants import (
    BANDS_TO_RESOLUTIONS, DATASTRIP_METADATA_ASSET_KEY, SENTINEL_PROVIDER,
    SENTINEL_LICENSE, SENTINEL_BANDS, SENTINEL_INSTRUMENTS,
    SENTINEL_CONSTELLATION, INSPIRE_METADATA_ASSET_KEY, L2A_IMAGE_PATHS,
    L1C_IMAGE_PATHS, SENTINEL2_PROPERTY_PREFIX as s2_prefix)

logger = logging.getLogger(__name__)

MGRS_PATTERN: Final[Pattern[str]] = re.compile(
    r"_T(\d{1,2})([CDEFGHJKLMNPQRSTUVWX])([ABCDEFGHJKLMNPQRSTUVWXYZ][ABCDEFGHJKLMNPQRSTUV])"
)

TCI_PATTERN: Final[Pattern[str]] = re.compile(r"[_/]TCI[_.]")
AOT_PATTERN: Final[Pattern[str]] = re.compile(r"[_/]AOT[_.]")
WVP_PATTERN: Final[Pattern[str]] = re.compile(r"[_/]WVP[_.]")
SCL_PATTERN: Final[Pattern[str]] = re.compile(r"[_/]SCL[_.]")

BAND_PATTERN: Final[Pattern[str]] = re.compile(r"[_/](B\w{2})")
IS_TCI_PATTERN: Final[Pattern[str]] = re.compile(r"[_/]TCI")
BAND_ID_PATTERN: Final[Pattern[str]] = re.compile(r"[_/](B\d[A\d])")
RESOLUTION_PATTERN: Final[Pattern[str]] = re.compile(r"(\w{2}m)")


@dataclass
class Metadata:
    scene_id: str
    cloudiness_percentage: Optional[float]
    extra_assets: Dict[str, pystac.Asset]
    geometry: Dict[str, Any]
    bbox: List[float]
    datetime: datetime
    platform: str
    metadata_dict: Dict[str, Any]
    image_media_type: str
    image_paths: List[str]
    epsg: int
    proj_bbox: List[float]
    resolution_to_shape: Dict[int, Tuple[int, int]]
    orbit_state: Optional[str] = None
    relative_orbit: Optional[int] = None


def create_item(granule_href: str,
                tolerance: float,
                additional_providers: Optional[List[pystac.Provider]] = None,
                read_href_modifier: Optional[ReadHrefModifier] = None,
                asset_href_prefix: Optional[str] = None) -> pystac.Item:
    """Create a STC Item from a Sentinel 2 granule.

    Arguments:
        granule_href: The HREF to the granule. This is expected to be a path
            to a SAFE archive, e.g. https://sentinel2l2a01.blob.core.windows.net/sentinel2-l2/01/C/CV/2016/03/27/S2A_MSIL2A_20160327T204522_N0212_R128_T01CCV_20210214T042702.SAFE,
            or a partial S3 object path, e.g. s3://sentinel-s2-l2a/tiles/10/S/DG/2018/12/31/0/
        tolerance: Determines the level of simplification of the geometry
        additional_providers: Optional list of additional providers to set into the Item
        read_href_modifier: A function that takes an HREF and returns a modified HREF.
            This can be used to modify a HREF to make it readable, e.g. appending
            an Azure SAS token or creating a signed URL.
        asset_href_prefix: The URL prefix to apply to the asset hrefs 

    Returns:
        pystac.Item: An item representing the Sentinel 2 scene
    """  # noqa

    if granule_href.lower().endswith(".safe"):
        metadata = metadata_from_safe_manifest(granule_href,
                                               read_href_modifier)
    else:
        metadata = metadata_from_granule_metadata(granule_href,
                                                  read_href_modifier,
                                                  tolerance)

    item = pystac.Item(id=metadata.scene_id,
                       geometry=metadata.geometry,
                       bbox=metadata.bbox,
                       datetime=metadata.datetime,
                       properties={})

    # --Common metadata--

    item.common_metadata.providers = [SENTINEL_PROVIDER]

    if additional_providers is not None:
        item.common_metadata.providers.extend(additional_providers)

    item.common_metadata.platform = metadata.platform.lower()
    item.common_metadata.constellation = SENTINEL_CONSTELLATION
    item.common_metadata.instruments = SENTINEL_INSTRUMENTS

    # --Extensions--

    # eo
    eo = EOExtension.ext(item, add_if_missing=True)
    eo.cloud_cover = metadata.cloudiness_percentage

    # sat
    if metadata.orbit_state or metadata.relative_orbit:
        sat = SatExtension.ext(item, add_if_missing=True)
        sat.orbit_state = OrbitState(
            metadata.orbit_state.lower()) if metadata.orbit_state else None
        sat.relative_orbit = metadata.relative_orbit

    # proj
    projection = ProjectionExtension.ext(item, add_if_missing=True)
    projection.epsg = metadata.epsg
    if projection.epsg is None:
        raise ValueError(
            f'Could not determine EPSG code for {granule_href}; which is required.'
        )

    # MGRS and Grid Extension
    mgrs_match = MGRS_PATTERN.search(metadata.scene_id)
    if mgrs_match and len(mgrs_groups := mgrs_match.groups()) == 3:
        mgrs = MgrsExtension.ext(item, add_if_missing=True)
        mgrs.utm_zone = int(mgrs_groups[0])
        mgrs.latitude_band = mgrs_groups[1]
        mgrs.grid_square = mgrs_groups[2]
        grid = GridExtension.ext(item, add_if_missing=True)
        grid.code = f"MGRS-{mgrs.utm_zone}{mgrs.latitude_band}{mgrs.grid_square}"
    else:
        logger.error(
            f'Error populating MGRS and Grid Extensions fields from ID: {metadata.scene_id}'
        )

    # View Extension
    view = ViewExtension.ext(item, add_if_missing=True)
    view.sun_azimuth = metadata.metadata_dict.get(
        f"{s2_prefix}:mean_solar_azimuth")
    if msz := metadata.metadata_dict.get(f"{s2_prefix}:mean_solar_zenith"):
        view.sun_elevation = 90 - msz

    # s2 properties
    item.properties.update(metadata.metadata_dict)

    # --Assets--

    image_assets = dict([
        image_asset_from_href(
            os.path.join(asset_href_prefix or granule_href,
                         image_path), metadata.resolution_to_shape,
            metadata.proj_bbox, metadata.image_media_type)
        for image_path in metadata.image_paths
    ])

    for key, asset in chain(image_assets.items(),
                            metadata.extra_assets.items()):
        assert key not in item.assets
        item.add_asset(key, asset)

    # --Links--

    item.links.append(SENTINEL_LICENSE)

    return item


def image_asset_from_href(
        asset_href: str,
        resolution_to_shape: Dict[int, Tuple[int, int]],
        proj_bbox: List[float],
        media_type: Optional[str] = None) -> Tuple[str, pystac.Asset]:
    logger.debug(f'Creating asset for image {asset_href}')

    _, ext = os.path.splitext(asset_href)
    if media_type is not None:
        asset_media_type = media_type
    else:
        if ext.lower() == '.jp2':
            asset_media_type = pystac.MediaType.JPEG2000
        elif ext.lower() in ['.tiff', '.tif']:
            asset_media_type = pystac.MediaType.GEOTIFF
        else:
            raise Exception(
                f'Must supply a media type for asset : {asset_href}')

    # Handle preview image

    if '_PVI' in asset_href:
        asset = pystac.Asset(href=asset_href,
                             media_type=asset_media_type,
                             title='True color preview',
                             roles=['data'])
        asset_eo = EOExtension.ext(asset)
        asset_eo.bands = [
            SENTINEL_BANDS['B04'], SENTINEL_BANDS['B03'], SENTINEL_BANDS['B02']
        ]
        return 'preview', asset

    # Extract gsd and proj info
    resolution = extract_gsd(asset_href)
    if resolution is None:
        # in Level-1C we can deduct the spatial resolution from the band ID or
        # asset name
        band_id_search = BAND_PATTERN.search(asset_href)
        if band_id_search:
            resolution = BANDS_TO_RESOLUTIONS[band_id_search.groups()[0]][0]
        elif IS_TCI_PATTERN.search(asset_href):
            resolution = 10

    shape = list(resolution_to_shape[int(resolution)])
    transform = transform_from_bbox(proj_bbox, shape)

    def set_asset_properties(_asset: pystac.Asset,
                             _band_gsd: Optional[int] = None):
        if _band_gsd:
            pystac.CommonMetadata(_asset).gsd = _band_gsd
        asset_projection = ProjectionExtension.ext(_asset)
        asset_projection.shape = shape
        asset_projection.bbox = proj_bbox
        asset_projection.transform = transform

    # Handle band image

    band_id_search = BAND_ID_PATTERN.search(asset_href)
    if band_id_search:
        try:
            band_id = band_id_search.group(1)
            asset_res = resolution
            band = SENTINEL_BANDS[band_id]
        except KeyError:
            # Level-1C have different names
            band_id = os.path.splitext(asset_href)[0].split('_')[-1]
            band = SENTINEL_BANDS[band_id]
            asset_res = BANDS_TO_RESOLUTIONS[band_id_search.groups()[0]][0]

        # Get the asset resolution from the file name.
        # If the asset resolution is the band GSD, then
        # include the gsd information for that asset. Otherwise,
        # do not include the GSD information in the asset
        # as this may be confusing for users given that the
        # raster spatial resolution and gsd will differ.
        # See https://github.com/radiantearth/stac-spec/issues/1096
        band_gsd: Optional[int] = None
        if asset_res == BANDS_TO_RESOLUTIONS[band_id][0]:
            asset_key = band_id
            band_gsd = asset_res
        else:
            # If this isn't the default resolution, use the raster
            # resolution in the asset key.
            # TODO: Use the raster extension and spatial_resolution
            # property to encode the spatial resolution of all assets.
            asset_key = f'{band_id}_{int(asset_res)}m'

        asset = pystac.Asset(href=asset_href,
                             media_type=asset_media_type,
                             title=f'{band.description} - {asset_res}m',
                             roles=['data'])

        asset_eo = EOExtension.ext(asset)
        asset_eo.bands = [SENTINEL_BANDS[band_id]]
        set_asset_properties(asset, band_gsd)
        return asset_key, asset

    # Handle auxiliary images
    elif TCI_PATTERN.search(asset_href):
        # True color
        asset = pystac.Asset(href=asset_href,
                             media_type=asset_media_type,
                             title='True color image',
                             roles=['visual'])
        asset_eo = EOExtension.ext(asset)
        asset_eo.bands = [
            SENTINEL_BANDS['B04'], SENTINEL_BANDS['B03'], SENTINEL_BANDS['B02']
        ]
        set_asset_properties(asset)

        maybe_res = extract_gsd(asset_href)
        asset_id = f'visual_{maybe_res}m' if maybe_res and maybe_res != 10 else "visual"
        return asset_id, asset

    elif AOT_PATTERN.search(asset_href):
        # Aerosol
        asset = pystac.Asset(href=asset_href,
                             media_type=asset_media_type,
                             title='Aerosol optical thickness (AOT)',
                             roles=['data'])
        set_asset_properties(asset)
        maybe_res = extract_gsd(asset_href)
        asset_id = mk_asset_id(maybe_res, "AOT")
        return asset_id, asset

    elif WVP_PATTERN.search(asset_href):
        # Water vapor
        asset = pystac.Asset(href=asset_href,
                             media_type=asset_media_type,
                             title='Water vapour (WVP)',
                             roles=['data'])
        set_asset_properties(asset)
        maybe_res = extract_gsd(asset_href)
        asset_id = mk_asset_id(maybe_res, "WVP")
        return asset_id, asset

    elif SCL_PATTERN.search(asset_href):
        # Classification map
        asset = pystac.Asset(href=asset_href,
                             media_type=asset_media_type,
                             title='Scene classification map (SCL)',
                             roles=['data'])
        set_asset_properties(asset)
        maybe_res = extract_gsd(asset_href)
        asset_id = mk_asset_id(maybe_res, "SCL")
        return asset_id, asset
    else:
        raise ValueError(f'Unexpected asset: {asset_href}')


def mk_asset_id(maybe_res: Optional[int], name: str):
    return f'{name}_{maybe_res}m' if maybe_res and maybe_res != 20 else name


# this is used for SAFE archive format
def metadata_from_safe_manifest(
        granule_href: str,
        read_href_modifier: Optional[ReadHrefModifier]) -> Metadata:
    safe_manifest = SafeManifest(granule_href, read_href_modifier)
    product_metadata = ProductMetadata(safe_manifest.product_metadata_href,
                                       read_href_modifier)
    granule_metadata = GranuleMetadata(safe_manifest.granule_metadata_href,
                                       read_href_modifier)
    extra_assets = dict([
        safe_manifest.create_asset(),
        product_metadata.create_asset(),
        granule_metadata.create_asset(),
        (INSPIRE_METADATA_ASSET_KEY,
         pystac.Asset(href=safe_manifest.inspire_metadata_href,
                      media_type=pystac.MediaType.XML,
                      roles=['metadata'])),
        (DATASTRIP_METADATA_ASSET_KEY,
         pystac.Asset(href=safe_manifest.datastrip_metadata_href,
                      media_type=pystac.MediaType.XML,
                      roles=['metadata'])),
    ])

    if safe_manifest.thumbnail_href is not None:
        extra_assets["preview"] = pystac.Asset(
            href=safe_manifest.thumbnail_href,
            media_type=pystac.MediaType.COG,
            roles=['thumbnail'])

    return Metadata(
        scene_id=product_metadata.scene_id,
        extra_assets=extra_assets,
        geometry=product_metadata.geometry,
        bbox=product_metadata.bbox,
        datetime=product_metadata.datetime,
        platform=product_metadata.platform,
        orbit_state=product_metadata.orbit_state,
        relative_orbit=product_metadata.relative_orbit,
        metadata_dict={
            **product_metadata.metadata_dict,
            **granule_metadata.metadata_dict
        },
        image_media_type=product_metadata.image_media_type,
        image_paths=product_metadata.image_paths,
        cloudiness_percentage=granule_metadata.cloudiness_percentage,
        epsg=granule_metadata.epsg,
        proj_bbox=granule_metadata.proj_bbox,
        resolution_to_shape=granule_metadata.resolution_to_shape)


# this is used for the Sinergise S3 format,
# e.g., s3://sentinel-s2-l1c/tiles/10/S/DG/2018/12/31/0/
def metadata_from_granule_metadata(
        granule_metadata_href: str,
        read_href_modifier: Optional[ReadHrefModifier],
        tolerance: float) -> Metadata:
    granule_metadata = GranuleMetadata(
        os.path.join(granule_metadata_href, 'metadata.xml'),
        read_href_modifier)
    tileinfo_metadata = TileInfoMetadata(
        os.path.join(granule_metadata_href, 'tileInfo.json'),
        read_href_modifier)

    geometry = make_shape(
        reproject_geom(f'epsg:{granule_metadata.epsg}', 'epsg:4326',
                       tileinfo_metadata.geometry)).simplify(tolerance)

    extra_assets = dict([
        granule_metadata.create_asset(),
        tileinfo_metadata.create_asset(),
    ])

    image_paths = L2A_IMAGE_PATHS if "_L2A_" in granule_metadata.scene_id else L1C_IMAGE_PATHS

    return Metadata(
        scene_id=granule_metadata.scene_id,
        extra_assets=extra_assets,
        metadata_dict={
            **granule_metadata.metadata_dict,
            **tileinfo_metadata.metadata_dict, f"{s2_prefix}:processing_baseline":
            granule_metadata.processing_baseline
        },
        cloudiness_percentage=granule_metadata.cloudiness_percentage,
        epsg=granule_metadata.epsg,
        proj_bbox=granule_metadata.proj_bbox,
        resolution_to_shape=granule_metadata.resolution_to_shape,
        geometry=mapping(geometry),
        bbox=geometry.bounds,
        datetime=tileinfo_metadata.datetime,
        platform=granule_metadata.platform,
        image_media_type=pystac.MediaType.JPEG2000,
        image_paths=image_paths)
