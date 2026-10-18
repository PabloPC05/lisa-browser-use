"""Ayudas para trámites con la Administración: sedes oficiales y búsqueda en el BOE.

Portado de lisa-agent (`src/tramites/official.ts` y `boe.ts`, commit 556fd39), que a su vez
los tomó de SpainAssist (packages/shared/src/official.ts y packages/agent/src/research/boe.ts,
commit 2de06d3, MIT © 2026 Pablo Rodríguez Caballero; ver NOTICE).
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

BOE_API_BASE = "https://www.boe.es/datosabiertos/api"

#: Dominios de las administraciones públicas españolas (y de la UE). Un dominio coincide por
#: sufijo entero: «sede.agenciatributaria.gob.es» con «gob.es»; «evilgob.es» no.
OFFICIAL_DOMAINS: tuple[tuple[str, str], ...] = (
    # Corporaciones de derecho público con sede electrónica (añadido en Lisa).
    ("registradores.org", "other"),
    # State (Administración General del Estado) and state agencies
    ("gob.es", "state"),
    ("boe.es", "state"),
    ("agenciatributaria.es", "state"),
    ("seg-social.es", "state"),
    ("sepe.es", "state"),
    ("dgt.es", "state"),
    ("policia.es", "state"),
    ("dnielectronico.es", "state"),
    ("citapreviadnie.es", "state"),
    ("fnmt.es", "state"),
    ("cert.fnmt.es", "state"),
    ("guardiacivil.es", "state"),
    ("mjusticia.es", "state"),
    ("justicia.es", "state"),
    ("catastro.minhap.es", "state"),
    ("redsara.es", "state"),
    ("correos.es", "state"),
    ("ine.es", "state"),
    ("imserso.es", "state"),
    ("mites.gob.es", "state"),
    ("exteriores.gob.es", "state"),
    # Autonomous communities
    ("juntadeandalucia.es", "regional"),
    ("aragon.es", "regional"),
    ("asturias.es", "regional"),
    ("caib.es", "regional"),
    ("gobiernodecanarias.org", "regional"),
    ("cantabria.es", "regional"),
    ("jcyl.es", "regional"),
    ("castillalamancha.es", "regional"),
    ("gencat.cat", "regional"),
    ("aoc.cat", "regional"),  # Catalan e-government consortium (VÀLid login)
    ("gva.es", "regional"),
    ("juntaex.es", "regional"),
    ("xunta.gal", "regional"),
    ("madrid.org", "regional"),
    ("comunidad.madrid", "regional"),
    ("carm.es", "regional"),
    ("navarra.es", "regional"),
    ("euskadi.eus", "regional"),
    ("larioja.org", "regional"),
    ("ceuta.es", "regional"),
    ("melilla.es", "regional"),
    # Municipalities: every one with 100,000+ inhabitants, by population (INE, Padrón at
    # 1 Jan 2025), then the provincial capitals and Mérida below that cutoff. Smaller towns are
    # covered by the provincial and shared sede platforms further down.
    ("madrid.es", "local"),  # Madrid
    ("barcelona.cat", "local"),  # Barcelona
    ("bcn.cat", "local"),
    ("valencia.es", "local"),  # València
    ("zaragoza.es", "local"),  # Zaragoza
    ("sevilla.org", "local"),  # Sevilla
    ("malaga.eu", "local"),  # Málaga (city; malaga.es is the Diputación)
    ("murcia.es", "local"),  # Murcia
    ("palma.cat", "local"),  # Palma (sede on palma.sedipualba.es)
    ("palma.es", "local"),
    ("laspalmasgc.es", "local"),  # Las Palmas de Gran Canaria
    ("alicante.es", "local"),  # Alicante
    ("bilbao.eus", "local"),  # Bilbao
    ("cordoba.es", "local"),  # Córdoba
    ("valladolid.es", "local"),  # Valladolid (also valladolid.gob.es)
    ("vigo.org", "local"),  # Vigo
    ("l-h.cat", "local"),  # L"Hospitalet de Llobregat
    ("gijon.es", "local"),  # Gijón
    ("vitoria-gasteiz.org", "local"),  # Vitoria-Gasteiz
    ("coruna.gal", "local"),  # A Coruña
    ("lacoruna.es", "local"),
    ("elche.es", "local"),  # Elche
    ("granada.org", "local"),  # Granada
    ("terrassa.cat", "local"),  # Terrassa
    ("badalona.cat", "local"),  # Badalona
    ("sabadell.cat", "local"),  # Sabadell
    ("oviedo.es", "local"),  # Oviedo
    ("cartagena.es", "local"),  # Cartagena (sede on cartagena.sedipualba.es)
    ("mostoles.es", "local"),  # Móstoles
    ("jerez.es", "local"),  # Jerez de la Frontera
    ("aytojerez.es", "local"),
    ("santacruzdetenerife.es", "local"),  # Santa Cruz de Tenerife
    ("pamplona.es", "local"),  # Pamplona
    ("almeriaciudad.es", "local"),  # Almería
    ("aytoalmeria.es", "local"),
    ("ayto-alcaladehenares.es", "local"),  # Alcalá de Henares
    ("leganes.org", "local"),  # Leganés
    ("getafe.es", "local"),  # Getafe
    ("ayto-fuenlabrada.es", "local"),  # Fuenlabrada
    ("fuenlabrada.es", "local"),
    ("donostia.eus", "local"),  # Donostia / San Sebastián
    ("castello.es", "local"),  # Castelló de la Plana
    ("aytoburgos.es", "local"),  # Burgos (city; burgos.es is the Diputación)
    ("ayto-alcorcon.es", "local"),  # Alcorcón
    ("santander.es", "local"),  # Santander
    ("albacete.es", "local"),  # Albacete (sede on albacete.sedipualba.es)
    ("aytolalaguna.es", "local"),  # San Cristóbal de La Laguna
    ("marbella.es", "local"),  # Marbella (sede on marbella.sedelectronica.es)
    ("logrono.es", "local"),  # Logroño
    ("aytobadajoz.es", "local"),  # Badajoz
    ("paeria.cat", "local"),  # Lleida
    ("aytosalamanca.es", "local"),  # Salamanca (sede on aytosalamanca.gob.es)
    ("tarragona.cat", "local"),  # Tarragona
    ("ayto-torrejon.es", "local"),  # Torrejón de Ardoz
    ("huelva.es", "local"),  # Huelva
    ("doshermanas.es", "local"),  # Dos Hermanas
    ("ayuntamientoparla.es", "local"),  # Parla
    ("mataro.cat", "local"),  # Mataró
    ("algeciras.es", "local"),  # Algeciras
    ("gramenet.cat", "local"),  # Santa Coloma de Gramenet
    ("aytoleon.es", "local"),  # León
    ("alcobendas.org", "local"),  # Alcobendas
    ("aytojaen.es", "local"),  # Jaén
    ("reus.cat", "local"),  # Reus
    ("aytoroquetas.org", "local"),  # Roquetas de Mar
    ("roquetasdemar.es", "local"),
    ("cadiz.es", "local"),  # Cádiz
    ("girona.cat", "local"),  # Girona
    ("ourense.gal", "local"),  # Ourense (sede on sede.ourense.gob.es)
    ("telde.es", "local"),  # Telde (sede on telde.sedelectronica.es)
    ("rivasciudad.es", "local"),  # Rivas-Vaciamadrid
    ("barakaldo.eus", "local"),  # Barakaldo
    ("santiagodecompostela.gal", "local"),  # Santiago de Compostela
    ("concellodelugo.gal", "local"),  # Lugo
    ("lugo.gal", "local"),
    ("lugo.es", "local"),
    # Provincial capitals under 100,000 inhabitants, and Mérida (capital of Extremadura)
    ("ayto-caceres.es", "local"),  # Cáceres
    ("caceres.es", "local"),
    ("guadalajara.es", "local"),  # Guadalajara
    ("toledo.es", "local"),  # Toledo
    ("pontevedra.gal", "local"),  # Pontevedra
    ("aytopalencia.es", "local"),  # Palencia
    ("ciudadreal.es", "local"),  # Ciudad Real
    ("merida.es", "local"),  # Mérida
    ("zamora.es", "local"),  # Zamora (sede on zamora.sedelectronica.es)
    ("avila.es", "local"),  # Ávila
    ("huesca.es", "local"),  # Huesca
    ("cuenca.es", "local"),  # Cuenca
    ("segovia.es", "local"),  # Segovia
    ("soria.es", "local"),  # Soria (sede on soria.sedelectronica.es)
    ("teruel.es", "local"),  # Teruel
    # Diputaciones provinciales and forales, and their sede platforms for small towns. Several
    # provinces host town sedes on each town's own domain (Cádiz, Huelva, Sevilla, Badajoz,
    # A Coruña): those can't be covered by a suffix.
    ("dipalme.org", "local"),  # Almería (towns: sede.dipalme.org)
    ("dipucadiz.es", "local"),  # Cádiz
    ("dipucordoba.es", "local"),  # Córdoba
    ("eprinsa.es", "local"),  # Córdoba"s IT company (towns: sede.eprinsa.es/<town>)
    ("dipgra.es", "local"),  # Granada
    ("diphuelva.es", "local"),  # Huelva
    ("dipujaen.es", "local"),  # Jaén
    ("malaga.es", "local"),  # Málaga (towns: sede.malaga.es/<town>)
    ("prpmalaga.es", "local"),  # Málaga"s tax office (Patronato de Recaudación)
    ("dipusevilla.es", "local"),  # Sevilla
    ("sedeelectronicadipusevilla.es", "local"),
    ("opaef.es", "local"),  # Sevilla"s tax office (OPAEF)
    ("dip-badajoz.es", "local"),  # Badajoz
    ("dip-caceres.es", "local"),  # Cáceres
    ("diputacionalicante.es", "local"),  # Alicante
    ("dipcas.es", "local"),  # Castellón
    ("dival.es", "local"),  # Valencia (towns: <town>.sede.dival.es)
    ("dipualba.es", "local"),  # Albacete
    ("dipucr.es", "local"),  # Ciudad Real (towns: sedescr.dipucr.es/<town>)
    ("dipucuenca.es", "local"),  # Cuenca
    ("dguadalajara.es", "local"),  # Guadalajara
    ("diputoledo.es", "local"),  # Toledo (sede on diputacion.toledo.gob.es)
    ("diputacionavila.es", "local"),  # Ávila
    ("burgos.es", "local"),  # Burgos (the Diputación, not the city)
    ("diputaciondeburgos.es", "local"),
    ("dipuleon.es", "local"),  # León
    ("diputaciondepalencia.es", "local"),  # Palencia
    ("dip-palencia.es", "local"),
    ("lasalina.es", "local"),  # Salamanca (sede on diputaciondesalamanca.gob.es)
    ("dipsegovia.es", "local"),  # Segovia
    ("dipsoria.es", "local"),  # Soria
    ("diputaciondevalladolid.es", "local"),  # Valladolid
    ("diputaciondezamora.es", "local"),  # Zamora
    ("dphuesca.es", "local"),  # Huesca
    ("dpteruel.es", "local"),  # Teruel
    ("dpz.es", "local"),  # Zaragoza
    ("diba.cat", "local"),  # Barcelona
    ("ddgi.cat", "local"),  # Girona
    ("diputaciolleida.cat", "local"),  # Lleida
    ("dipta.cat", "local"),  # Tarragona (towns: actio.dipta.cat)
    ("dacoruna.gal", "local"),  # A Coruña (town sedes: sede-<town>.dacoruna.gal)
    ("deputacionlugo.gal", "local"),  # Lugo
    ("deputacionlugo.org", "local"),
    ("depourense.gal", "local"),  # Ourense
    ("depourense.es", "local"),
    ("depo.gal", "local"),  # Pontevedra
    ("araba.eus", "local"),  # Álava (towns: udalenegoitza.araba.eus/<town>)
    ("bizkaia.eus", "local"),  # Bizkaia
    ("bizkaia.net", "local"),
    ("ebizkaia.eus", "local"),
    ("gipuzkoa.eus", "local"),  # Gipuzkoa (towns: udal.egoitza.gipuzkoa.eus)
    ("gipuzkoa.net", "local"),
    # Cabildos (Canarias) and Consells Insulars (Illes Balears)
    ("sede.grancanaria.com", "local"),  # Gran Canaria (not tourism)
    ("tenerife.es", "local"),  # Tenerife
    ("cabildodelanzarote.com", "local"),  # Lanzarote
    ("cabildofuer.es", "local"),  # Fuerteventura
    ("cabildodelapalma.es", "local"),  # La Palma
    ("lagomera.es", "local"),  # La Gomera
    ("elhierro.es", "local"),  # El Hierro
    ("conselldemallorca.es", "local"),  # Mallorca
    ("conselldemallorca.cat", "local"),
    ("conselldemallorca.net", "local"),
    ("secimallorca.net", "local"),
    ("cime.es", "local"),  # Menorca
    ("www.carpetaciutadana.org", "local"),  # Menorca"s shared sede (Consell + towns)
    ("conselldeivissa.es", "local"),  # Eivissa
    ("conselldeivissa.cat", "local"),
    ("conselldeformentera.cat", "local"),  # Formentera
    ("consellinsulardeformentera.cat", "local"),
    ("consellformentera.com", "local"),
    # Shared sede platforms run by public bodies. Each subdomain is one entity's sede.
    ("sedipualba.es", "local"),  # Diputación de Albacete; 1,000+ bodies
    ("seu-e.cat", "local"),  # Consorci AOC: seu-e.cat/web/<town>
    ("seu.cat", "local"),  # Consorci AOC: e-TAULER, tràmits
    # Shared sede platforms run by a private vendor: esPublico Servicios para la Administración
    # S.A. (Gestiona). It issues one subdomain per customer, which are town councils, diputaciones
    # and other public-sector bodies (a few are publicly funded associations, e.g. local action
    # groups). Unknown names show a placeholder sede, not someone else's page. Trust tradeoff:
    # we rely on esPublico only issuing subdomains to its public customers.
    ("sedelectronica.es", "local"),
    ("sedelectronica.gal", "local"),
    ("eadministracio.cat", "local"),
    ("egoitzaelektronikoa.eus", "local"),
    # EU
    ("europa.eu", "eu"),
)


def _matches(host: str, suffix: str) -> bool:
    host = host.lower().rstrip(".")
    return host == suffix or host.endswith("." + suffix)


def official_site(url: str) -> dict[str, Any]:
    """Si `url` (solo https) es de una administración pública y de qué nivel."""
    try:
        parts = urlsplit(url if "://" in url else "https://" + url)
        host = parts.hostname or ""
    except ValueError:
        return {"official": False}
    if parts.scheme != "https" or not host:
        return {"official": False}
    for suffix, level in OFFICIAL_DOMAINS:
        if _matches(host, suffix):
            return {"official": True, "domain": suffix, "level": level}
    return {"official": False}


_STOPWORDS = frozenset(
    "a al con de del el en la las lo los o para por que se su sus un una unos unas y e u and or not".split()
)
_NOT_WORD = re.compile(r"[^\w\s/.-]|_")


def build_boe_query(text: str, field: str = "titulo", in_force_only: bool = True) -> str:
    """La consulta del BOE: las palabras unidas con «and» dentro del campo elegido."""
    phrases: list[str] = []

    def keep_phrase(match: re.Match) -> str:
        clean = re.sub(r"\s+", " ", _NOT_WORD.sub(" ", match.group(1))).strip()
        if clean:
            phrases.append(f'"{clean}"')
        return " "

    rest = re.sub(r'"([^"]{2,120})"', keep_phrase, text)
    words = [w for w in _NOT_WORD.sub(" ", rest).split() if w.lower() not in _STOPWORDS]
    terms = (phrases + words)[:12]
    if not terms:
        raise ValueError("La búsqueda necesita al menos una palabra con significado")
    clause = f"{field}:({' and '.join(terms)})"
    return f"{clause} and vigencia_agotada:N" if in_force_only else clause


def _iso(value: Any) -> str | None:
    if isinstance(value, str) and re.match(r"\d{8}", value):
        return f"{value[:4]}-{value[4:6]}-{value[6:8]}"
    return None


def _text(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and isinstance(value.get("texto"), str):
        return value["texto"]
    return None


def search_boe(
    query: str, *, field: str = "titulo", in_force_only: bool = True, newest_first: bool = False, limit: int = 8,
    base_url: str = BOE_API_BASE, client: httpx.Client | None = None, timeout: float = 20.0,
) -> list[dict[str, Any]]:
    """Legislación consolidada del BOE por su API de datos abiertos (sin clave)."""
    body: dict[str, Any] = {"query": {"query_string": {"query": build_boe_query(query, field, in_force_only)}}}
    if newest_first:
        body["sort"] = [{"fecha_publicacion": "desc"}]
    limit = min(20, max(1, int(limit)))
    url = f"{base_url.rstrip('/')}/legislacion-consolidada?query={quote(json.dumps(body, ensure_ascii=False))}&limit={limit}"
    own = client is None
    client = client or httpx.Client(timeout=timeout)
    try:
        res = client.get(url, headers={"accept": "application/json"})
    finally:
        if own:
            client.close()
    if res.status_code != 200:
        raise RuntimeError(f"la API del BOE respondió {res.status_code}")
    data = res.json()
    status = data.get("status") or {}
    if status.get("code") and str(status["code"]) != "200":
        raise RuntimeError(f"error de la API del BOE: {status.get('text') or status['code']}")
    items = data.get("data")
    items = items if isinstance(items, list) else [items] if items else []
    laws = []
    for item in items:
        law_id, title = item.get("identificador"), item.get("titulo")
        if not isinstance(law_id, str) or not isinstance(title, str):
            continue
        law = {
            "id": law_id, "title": title, "rank": _text(item.get("rango")),
            "department": _text(item.get("departamento")), "scope": _text(item.get("ambito")),
            "published_at": _iso(item.get("fecha_publicacion")), "enacted_at": _iso(item.get("fecha_disposicion")),
            "in_force": item.get("vigencia_agotada") != "S", "consolidation": _text(item.get("estado_consolidacion")),
            "url": item.get("url_html_consolidada") if isinstance(item.get("url_html_consolidada"), str)
            else f"https://www.boe.es/buscar/act.php?id={quote(law_id)}",
            "eli": item.get("url_eli") if isinstance(item.get("url_eli"), str) else None,
        }
        laws.append({k: v for k, v in law.items() if v is not None})
    return laws
