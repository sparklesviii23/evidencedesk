from .base import Adapter
from .crossref import CrossrefAdapter, UnpaywallAdapter
from .ctgov import CTGovAdapter
from .europepmc import EuropePMCAdapter
from .licensed import EmbaseAdapter, ScienceDirectAdapter, ScopusAdapter, WoSAdapter
from .openalex import OpenAlexAdapter
from .pubmed import PubMedAdapter

ADAPTERS: dict[str, Adapter] = {a.name: a for a in [
    PubMedAdapter(), EuropePMCAdapter(), CTGovAdapter(), OpenAlexAdapter(), ScopusAdapter(), EmbaseAdapter(),
    WoSAdapter(), ScienceDirectAdapter(), CrossrefAdapter(), UnpaywallAdapter()]}
SEARCH_SOURCES = [n for n, a in ADAPTERS.items() if a.role == "search"]


def get_adapter(name: str) -> Adapter:
    return ADAPTERS[name]
