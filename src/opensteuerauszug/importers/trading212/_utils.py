"""Generic ISIN utilities used by the Trading212 importer."""
import logging
import re
from typing import Optional

from opensteuerauszug.model.ech0196 import ISINType

logger = logging.getLogger(__name__)


def _validate_isin(isin: Optional[str]) -> Optional[ISINType]:
    """Return the ISIN if it matches the eCH-0196 pattern, else None."""
    if not isin:
        return None
    if re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}[0-9]{1}", isin):
        return ISINType(isin)
    logger.debug("Invalid ISIN format '%s', omitting from output", isin)
    return None


def _country_from_isin(isin: Optional[str]) -> Optional[str]:
    """Extract the 2-letter issuer country code from an ISIN.

    Returns ``None`` for international/special ISIN prefixes that do not
    correspond to a country (e.g. ``XS`` for Eurobonds) and when no ISIN
    is available.
    """
    if not isin or len(isin) < 2:
        return None
    prefix = isin[:2].upper()
    # Prefixes starting with 'X' (XS, XC, XF, …) and 'QZ' are reserved for
    # international / supra-national instruments, not country codes.
    if prefix[0] == "X" or prefix == "QZ":
        return None
    return prefix if re.fullmatch(r"[A-Z]{2}", prefix) else None
