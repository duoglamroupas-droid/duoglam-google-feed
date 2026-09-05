"""
Gera um feed XML (formato RSS 2.0 do Google Shopping) a partir dos produtos
da Loja Integrada, para ser usado como "Inserir um link para o arquivo" no
Google Merchant Center.

Este script é feito para rodar via GitHub Actions (agendado diariamente),
sem precisar de Google Cloud / billing.

Variável de ambiente necessária:
  LI_PERSONAL_TOKEN -> Personal token da Loja Integrada

Saída:
  feed.xml (na raiz do repositório)
"""

import os
import re
import time
import logging
from xml.sax.saxutils import escape

import requests

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("feed-generator")

LI_BASE_URL = "https://api.awsli.com.br/api/v1"
LI_TOKEN = os.environ.get("LI_PERSONAL_TOKEN", "")
LI_PAGE_LIMIT = 50

CURRENCY_CODE = "BRL"
SELLABLE_TYPES = {"normal", "atributo_opcao"}
REQUEST_TIMEOUT = 30
SLEEP_BETWEEN_CALLS = 0.15

STORE_NAME = "Duo Glam"
STORE_URL = "https://www.duoglam.com.br"

_brand_cache = {}


def li_headers():
    return {
        "Authorization": f"Basic {LI_TOKEN}",
        "Accept": "application/json",
    }


def li_list_products():
    offset = 0
    while True:
        url = f"{LI_BASE_URL}/produto"
        params = {"limit": LI_PAGE_LIMIT, "offset": offset}
        resp = requests.get(url, headers=li_headers(), params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()

        for obj in data.get("objects", []):
            yield obj

        meta = data.get("meta", {})
        if not meta.get("next"):
            break
        offset += LI_PAGE_LIMIT
        time.sleep(SLEEP_BETWEEN_CALLS)


def li_get_product_detail(product_id):
    url = f"{LI_BASE_URL}/produto/{product_id}"
    resp = requests.get(url, headers=li_headers(), timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def li_get_brand_name(marca_resource_uri):
    if not marca_resource_uri:
        return None
    if marca_resource_uri in _brand_cache:
        return _brand_cache[marca_resource_uri]
    try:
        url = f"https://api.awsli.com.br{marca_resource_uri}"
        resp = requests.get(url, headers=li_headers(), timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        nome = resp.json().get("nome")
        _brand_cache[marca_resource_uri] = nome
        return nome
    except Exception as exc:
        logger.warning("Falha ao buscar marca %s: %s", marca_resource_uri, exc)
        _brand_cache[marca_resource_uri] = None
        return None


def strip_html(html_text):
    if not html_text:
        return ""
    text = re.sub(r"<[^>]+>", " ", html_text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def format_price(value):
    return f"{float(value):.2f} {CURRENCY_CODE}"


def build_item_xml(detail):
    sku = detail.get("sku")
    if not sku:
        return None

    nome = detail.get("nome") or detail.get("apelido") or sku
    link = detail.get("url")
    if not link:
        return None
    if not link.startswith("http"):
        link = STORE_URL.rstrip("/") + "/" + link.lstrip("/")

    descricao = strip_html(detail.get("descricao_completa")) or nome

    preco_cheio = detail.get("preco_cheio")
    preco_promocional = detail.get("preco_promocional")
    if preco_cheio is None:
        return None

    price_value = preco_cheio
    sale_price_value = None
    if preco_promocional and float(preco_promocional) > 0 and float(preco_promocional) < float(preco_cheio):
        sale_price_value = preco_promocional

    estoque_gerenciado = detail.get("estoque_gerenciado", False)
    estoque_quantidade = detail.get("estoque_quantidade") or 0
    if estoque_gerenciado:
        availability = "in stock" if estoque_quantidade > 0 else "out of stock"
    else:
        availability = "in stock"

    imagem_principal = detail.get("imagem_principal") or {}
    image_link = imagem_principal.get("grande")
    if not image_link:
        return None

    additional_images = []
    for img in detail.get("imagens", []):
        if img.get("principal"):
            continue
        if img.get("grande"):
            additional_images.append(img["grande"])
    additional_images = additional_images[:10]

    brand = li_get_brand_name(detail.get("marca"))
    gtin = detail.get("gtin")
    condition = "used" if detail.get("usado") else "new"

    parts = ["  <item>"]
    parts.append(f"    <g:id>{escape(sku)}</g:id>")
    parts.append(f"    <title>{escape(nome[:150])}</title>")
    parts.append(f"    <description>{escape(descricao[:5000])}</description>")
    parts.append(f"    <link>{escape(link)}</link>")
    parts.append(f"    <g:image_link>{escape(image_link)}</g:image_link>")
    for extra_img in additional_images:
        parts.append(f"    <g:additional_image_link>{escape(extra_img)}</g:additional_image_link>")
    parts.append(f"    <g:availability>{availability}</g:availability>")
    parts.append(f"    <g:price>{format_price(price_value)}</g:price>")
    if sale_price_value:
        parts.append(f"    <g:sale_price>{format_price(sale_price_value)}</g:sale_price>")
    parts.append(f"    <g:condition>{condition}</g:condition>")
    if brand:
        parts.append(f"    <g:brand>{escape(brand)}</g:brand>")
    if gtin:
        parts.append(f"    <g:gtin>{escape(gtin)}</g:gtin>")
    parts.append("  </item>")
    return "\n".join(parts)


def diagnosticar_motivo(detail):
    if not detail.get("sku"):
        return "sem SKU"
    if not detail.get("url"):
        return "sem URL"
    if detail.get("preco_cheio") is None:
        return "sem preco_cheio"
    imagem_principal = detail.get("imagem_principal") or {}
    if not imagem_principal.get("grande"):
        return "sem imagem_principal"
    return "motivo desconhecido"


def generate_feed():
    items_xml = []
    total_vistos = 0
    total_incluidos = 0
    pulados_inativo_removido = 0
    pulados_tipo = 0
    pulados_sem_dados = []
    erros = []

    for resumo in li_list_products():
        total_vistos += 1

        if resumo.get("removido") or not resumo.get("ativo"):
            pulados_inativo_removido += 1
            continue
        if resumo.get("tipo") not in SELLABLE_TYPES:
            pulados_tipo += 1
            continue

        try:
            detail = li_get_product_detail(resumo["id"])
            item_xml = build_item_xml(detail)
            if item_xml:
                items_xml.append(item_xml)
                total_incluidos += 1
            else:
                motivo = diagnosticar_motivo(detail)
                pulados_sem_dados.append((detail.get("sku"), detail.get("id"), motivo))
        except Exception as exc:
            erros.append((resumo.get("id"), str(exc)))
            logger.warning("Erro no produto %s: %s", resumo.get("id"), exc)

        time.sleep(SLEEP_BETWEEN_CALLS)

    logger.info("Vistos: %d, Incluídos no feed: %d", total_vistos, total_incluidos)
    logger.info("Pulados (inativo/removido): %d", pulados_inativo_removido)
    logger.info("Pulados (tipo não vendável, ex: 'atributo' pai): %d", pulados_tipo)
    logger.info("Pulados (faltando dado obrigatório): %d", len(pulados_sem_dados))
    for sku, pid, motivo in pulados_sem_dados:
        logger.info("  -> SKU=%s ID=%s motivo=%s", sku, pid, motivo)
    if erros:
        logger.info("Erros de requisição: %d", len(erros))
        for pid, err in erros:
            logger.info("  -> ID=%s erro=%s", pid, err)

    feed = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss xmlns:g="http://base.google.com/ns/1.0" version="2.0">\n'
        "<channel>\n"
        f"  <title>{escape(STORE_NAME)}</title>\n"
        f"  <link>{escape(STORE_URL)}</link>\n"
        "  <description>Feed de produtos para o Google Merchant Center</description>\n"
        + "\n".join(items_xml)
        + "\n</channel>\n</rss>\n"
    )
    return feed


if __name__ == "__main__":
    if not LI_TOKEN:
        raise SystemExit("LI_PERSONAL_TOKEN não configurado")

    feed_content = generate_feed()
    with open("feed.xml", "w", encoding="utf-8") as f:
        f.write(feed_content)
    logger.info("feed.xml gerado com sucesso")
