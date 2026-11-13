#!/usr/bin/env python3
"""Rastreador de preços.

Lê products.json, consulta cada oferta (produto + loja), guarda o histórico em
data/history.json e avisa no Telegram quando algo relevante acontece.

Regras de negócio (ajustáveis em products.json -> "configuracoes"):
  - queda_minima_pct:      queda mínima (em %) para avisar. Padrão 1.
  - variacao_suspeita_pct: salto (em %) a partir do qual uma leitura é tratada
                           como suspeita e só entra no histórico depois de
                           confirmada pela coleta seguinte. Padrão 50.
Cada produto pode ter o seu próprio "queda_minima_pct".
"""
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

RAIZ = Path(__file__).parent
ARQ_PRODUTOS = RAIZ / "products.json"
ARQ_HISTORICO = RAIZ / "data" / "history.json"
FUSO = timezone(timedelta(hours=-3))  # horário de Brasília

PADROES = {"queda_minima_pct": 1.0, "variacao_suspeita_pct": 50.0}
TOLERANCIA_CONFIRMACAO_PCT = 5.0  # leitura seguinte "perto" da suspeita = confirmada

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9",
}

PRECO = r"([\d.]+,\d{2})"


# ---------- utilitários ----------

def brl_para_float(txt):
    """'4.325,00' -> 4325.0"""
    return float(txt.replace(".", "").replace(",", "."))


def formatar(valor):
    """4325.0 -> 'R$ 4.325,00'"""
    if valor is None:
        return "—"
    s = f"{valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {s}"


def slug(texto):
    """'Fogão Brastemp 4 bocas' -> 'fogao-brastemp-4-bocas' (sem acentos)."""
    sem_acento = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", sem_acento.lower()).strip("-")


def chave_oferta(produto, oferta):
    """Identificador da oferta no histórico: produto + loja."""
    return oferta.get("id") or f"{produto['id']}@{slug(oferta['loja'])}"


def melhor_preco(reg):
    """Menor valor pago hoje: o Pix, se existir e for menor, senão o preço atual."""
    candidatos = [v for v in (reg.get("preco_atual"), reg.get("preco_pix")) if v]
    return min(candidatos) if candidatos else None


def validos(hist):
    """Leituras confiáveis: sem erro e sem a marca de suspeita."""
    return [r for r in hist if not r.get("erro") and not r.get("suspeita")]


# ---------- regras de negócio (funções puras, fáceis de testar) ----------

def base_alerta(leituras, pct):
    """Preço de referência para decidir se houve queda relevante.

    Sobe junto com o preço, mas só desce quando a queda acumulada atinge o
    limite. Assim, várias quedinhas de centavos não passam despercebidas
    para sempre nem geram aviso a cada uma.
    """
    base = None
    for r in leituras:
        p = melhor_preco(r)
        if base is None or p > base:
            base = p
        elif p <= base * (1 - pct / 100):
            base = p
    return base


def classificar_leitura(hist, reg, limite_suspeita_pct):
    """Decide se a leitura nova é suspeita ou confirma uma suspeita anterior.

    Retorna (suspeita_nova, suspeita_confirmada). Pode alterar `hist`
    (remove a marca da leitura anterior quando confirmada).
    """
    if reg.get("erro"):
        return False, None

    leituras_ok = [r for r in hist if not r.get("erro")]
    ultima = leituras_ok[-1] if leituras_ok else None

    # 1) a leitura anterior era suspeita e esta a confirma?
    if ultima and ultima.get("suspeita"):
        p_susp, p_novo = melhor_preco(ultima), melhor_preco(reg)
        if abs(p_novo - p_susp) / p_susp * 100 <= TOLERANCIA_CONFIRMACAO_PCT:
            ultima.pop("suspeita")
            return False, ultima

    # 2) esta leitura destoa muito da última leitura confiável?
    confiaveis = validos(hist)
    if confiaveis:
        ref = melhor_preco(confiaveis[-1])
        desvio = abs(melhor_preco(reg) - ref) / ref * 100
        if desvio > limite_suspeita_pct:
            reg["suspeita"] = True
            return True, None
    return False, None


def avaliar_avisos(nome, url, anteriores, reg, pct, alvo):
    """Gera os avisos de uma leitura nova, comparando com as anteriores."""
    avisos = []
    if not anteriores:
        return avisos
    p = melhor_preco(reg)
    base = base_alerta(anteriores, pct)
    if p <= base * (1 - pct / 100):
        queda = (1 - p / base) * 100
        avisos.append(
            f"📉 Baixou {f'{queda:.1f}'.replace('.', ',')}%: {nome}\n"
            f"{formatar(base)} → {formatar(p)}\n{url}"
        )
    minimo = min(melhor_preco(r) for r in anteriores)
    if len(anteriores) >= 2 and p <= minimo * (1 - pct / 100):
        avisos.append(
            f"🏆 Menor preço registrado: {nome}\n"
            f"{formatar(p)} (antes: {formatar(minimo)})\n{url}"
        )
    if alvo and p <= alvo < melhor_preco(anteriores[-1]):
        avisos.append(
            f"🎯 Bateu sua meta de {formatar(alvo)}: {nome}\n"
            f"Agora: {formatar(p)}\n{url}"
        )
    return avisos


# ---------- leitura das páginas ----------

def numero(valor):
    """'3689.00' | '3.689,00' | 'R$ 3.689,00' | 3689 -> 3689.0 (None se não der)."""
    txt = str(valor).strip().replace("R$", "").replace(" ", "")
    if not txt:
        return None
    if "," in txt:  # formato brasileiro
        txt = txt.replace(".", "").replace(",", ".")
    try:
        v = float(txt)
    except ValueError:
        return None
    return v if v > 0 else None


def preco_da_meta(soup):
    """Preço publicado em meta tags padronizadas (Open Graph / schema.org)."""
    candidatos = (
        {"property": "product:price:amount"},
        {"name": "product:price:amount"},
        {"property": "og:price:amount"},
        {"itemprop": "price"},
    )
    for attrs in candidatos:
        tag = soup.find(attrs=attrs)
        if tag:
            v = numero(tag.get("content") or tag.get_text())
            if v:
                return v
    return None


def precos_jsonld(soup):
    """Preços de blocos JSON-LD (schema.org Offer/AggregateOffer), na ordem em que aparecem."""
    achados = []
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            dados = json.loads(tag.string or tag.get_text())
        except ValueError:
            continue
        fila = [dados]
        while fila:
            x = fila.pop(0)
            if isinstance(x, list):
                fila.extend(x)
            elif isinstance(x, dict):
                tipo = x.get("@type")
                tipos = tipo if isinstance(tipo, list) else [tipo]
                if "Offer" in tipos or "AggregateOffer" in tipos:
                    for chave in ("price", "lowPrice"):
                        v = numero(x[chave]) if x.get(chave) not in (None, "") else None
                        if v:
                            achados.append(v)
                            break
                fila.extend(v for v in x.values() if isinstance(v, (dict, list)))
    return achados


def ler_pagamento(texto, dados):
    """Acrescenta Pix e parcelamento sem juros, quando aparecem no texto da página."""
    m = re.search(r"R\$\s*" + PRECO + r"\s*no\s*Pix", texto, re.I)
    if m:
        dados["preco_pix"] = brl_para_float(m.group(1))
    m = re.search(r"(\d+)\s*x\s*de\s*R\$\s*" + PRECO + r"\s*sem\s*juros", texto, re.I)
    if m:
        parcelas = int(m.group(1))
        valor = brl_para_float(m.group(2))
        dados["parcelas_sem_juros"] = parcelas
        dados["valor_parcela"] = valor
        dados["total_parcelado"] = round(parcelas * valor, 2)


def extrair_generico(html):
    """Leitor para qualquer loja que publique o preço de forma padronizada.

    Tenta, nesta ordem: meta tags de produto e JSON-LD (schema.org).
    Se a página não tiver nenhum dos dois, devolve {} (não chutamos preço).
    """
    soup = BeautifulSoup(html, "html.parser")
    preco = preco_da_meta(soup)
    if not preco:
        ld = precos_jsonld(soup)
        preco = ld[0] if ld else None
    if not preco:
        return {}
    dados = {"preco_atual": preco}
    ler_pagamento(soup.get_text(" ", strip=True), dados)
    return dados


def extrair_brastemp(html):
    """Brastemp: leitor genérico + o preço 'de' (antes da etiqueta 'Oferta')."""
    dados = extrair_generico(html)
    if dados:
        texto = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
        m = re.search(PRECO + r"\s*(?:\S+\s*){0,3}Oferta", texto)
        if m:
            dados["preco_original"] = brl_para_float(m.group(1))
    return dados


def extrair_amazon(html):
    """Amazon: o preço fica em blocos próprios da página (não usa o formato padrão)."""
    soup = BeautifulSoup(html, "html.parser")
    preco = None
    campo = soup.select_one("input#twister-plus-price-data-price")
    if campo and campo.get("value"):
        preco = numero(campo["value"])
    if not preco:
        for seletor in (
            "#corePriceDisplay_desktop_feature_div .a-price .a-offscreen",
            "#corePrice_feature_div .a-price .a-offscreen",
            "#corePrice_desktop .a-price .a-offscreen",
            "#apex_desktop .a-price .a-offscreen",
            "#priceblock_dealprice",
            "#priceblock_ourprice",
        ):
            tag = soup.select_one(seletor)
            if tag and "," in tag.get_text():
                preco = numero(tag.get_text())
                if preco:
                    break
    if not preco:
        m = re.search(r'"priceAmount"\s*:\s*([0-9]+(?:\.[0-9]+)?)', html)
        preco = numero(m.group(1)) if m else None
    if not preco:
        return {}
    dados = {"preco_atual": preco}
    de = soup.select_one(".basisPrice .a-offscreen")
    if de and "," in de.get_text():
        v = numero(de.get_text())
        if v and v > preco:
            dados["preco_original"] = v
    ler_pagamento(soup.get_text(" ", strip=True), dados)
    return dados


# Lojas com leitor específico. Todas as outras usam o leitor genérico.
EXTRATORES = {"brastemp": extrair_brastemp, "amazon": extrair_amazon}

# Nomes bonitos para lojas conhecidas. Qualquer outro site recebe o nome do domínio.
NOMES_LOJAS = {
    "brastemp.com.br": "Brastemp",
    "fastshop.com.br": "Fast Shop",
    "magazineluiza.com.br": "Magazine Luiza",
    "mercadolivre.com.br": "Mercado Livre",
    "casasbahia.com.br": "Casas Bahia",
    "kabum.com.br": "KaBuM",
    "amazon.com.br": "Amazon",
    "a.co": "Amazon",          # atalho do botão "compartilhar" da Amazon
    "amzn.to": "Amazon",
    "shopee.com.br": "Shopee",
    "carrefour.com.br": "Carrefour",
}


def loja_por_url(url):
    """Nome da loja pelo endereço do link (None se o link não tiver endereço válido)."""
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return None
    for dominio, loja in NOMES_LOJAS.items():
        if host == dominio or host.endswith("." + dominio):
            return loja
    partes = host.split(".")
    if len(partes) >= 3 and partes[-1] == "br" and partes[-2] in ("com", "net", "org"):
        base = partes[-3]
    else:
        base = partes[-2] if len(partes) >= 2 else partes[0]
    return base.capitalize()


BLOQUEIOS = (403, 429, 503)  # respostas típicas de "não quero robô"


def baixar_via_servico(url):
    """Pede a página a um serviço de leitura (ScraperAPI), que acessa por outro caminho.
    Só é usado quando a loja bloqueia o acesso direto e a chave SCRAPERAPI_KEY existe."""
    chave = os.environ.get("SCRAPERAPI_KEY")
    if not chave:
        return None, "sem chave do serviço"
    params = {"api_key": chave, "url": url}
    pais = os.environ.get("SCRAPERAPI_PAIS")  # opcional; planos pagos permitem "br"
    if pais:
        params["country_code"] = pais
    try:
        r = requests.get("https://api.scraperapi.com/", params=params, timeout=75)
    except requests.RequestException as e:
        # só o tipo do erro: a mensagem completa poderia conter a chave
        return None, f"serviço: {type(e).__name__}"
    if r.status_code == 200:
        print("  (lido via serviço de leitura: 1+ crédito gasto)")
        return r.text, None
    return None, f"serviço: HTTP {r.status_code}"


def endereco_definitivo(url, html):
    """Links de atalho (a.co, amzn.to) mudam de destino; guardamos o endereço real.
    Na Amazon, o endereço fica reduzido ao código do produto (ASIN)."""
    host = (urlparse(url).hostname or "").lower()
    if host in ("a.co", "amzn.to") and html:
        tag = BeautifulSoup(html, "html.parser").find("link", rel="canonical")
        if tag and tag.get("href"):
            url = tag["href"]
    m = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", url)
    if m and "amazon." in (urlparse(url).hostname or ""):
        return f"https://www.amazon.com.br/dp/{m.group(1)}"
    return url


def baixar(url):
    ultimo_erro = "falha desconhecida"
    bloqueado = False
    for tentativa in range(3):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            if r.status_code == 200:
                return r.text, None
            ultimo_erro = f"HTTP {r.status_code}"
            if r.status_code in BLOQUEIOS:
                bloqueado = True
                break  # insistir não adianta; passa para o serviço
        except requests.RequestException as e:
            ultimo_erro = type(e).__name__
        time.sleep(3 * (tentativa + 1))

    if bloqueado and os.environ.get("SCRAPERAPI_KEY"):
        html, erro_servico = baixar_via_servico(url)
        if html:
            return html, None
        return None, f"{ultimo_erro}, e o serviço falhou ({erro_servico})"
    return None, ultimo_erro


def inspecionar(html, max_valores=8):
    """Resume o que o robô enxerga na página (para diagnóstico, em linhas de texto)."""
    soup = BeautifulSoup(html, "html.parser")
    texto = soup.get_text(" ", strip=True)
    linhas = []
    titulo = soup.title.get_text().strip()[:90] if soup.title else "(sem título)"
    linhas.append(f"título da página: {titulo}")
    meta = preco_da_meta(soup)
    linhas.append(f"meta de preço: {formatar(meta) if meta else 'não encontrada'}")
    ld = precos_jsonld(soup)
    linhas.append("JSON-LD (schema.org): " + (", ".join(formatar(v) for v in ld[:3]) if ld else "não encontrado"))
    parc = [m.group(0).strip() for m in re.finditer(r"\d+\s*x\s*de\s*R\$\s*" + PRECO + r"[^.]{0,20}", texto, re.I)][:3]
    linhas.append("parcelamentos no texto: " + (" | ".join(parc) if parc else "nenhum"))
    if re.search(r"captcha|are you a robot|acesso negado|access denied|verifique que você", texto, re.I):
        linhas.append("⚠️ a página parece pedir verificação anti-robô")
    valores = list(re.finditer(r"R\$\s*" + PRECO, texto))
    linhas.append(f"valores 'R$' no texto: {len(valores)}")
    for m in valores[:max_valores]:
        ini = max(0, m.start() - 30)
        linhas.append("  - " + texto[ini : m.end() + 25].replace("\n", " "))
    return linhas


def diagnostico(rotulo, html):
    """Mostra no log o que o robô enxergou. Só roda com DEBUG=1 (execução manual)."""
    print(f"\n===== DIAGNÓSTICO: {rotulo} =====")
    for linha in inspecionar(html):
        print(linha)
    try:
        pais = requests.get("https://ipinfo.io/country", timeout=15).text.strip()
        print(f"País de onde o robô está acessando: {pais}")
    except requests.RequestException:
        print("País de onde o robô está acessando: não consegui descobrir")
    print("=====\n")


def coletar(oferta, rotulo):
    agora = datetime.now(FUSO).isoformat(timespec="minutes")
    extrator = EXTRATORES.get(oferta["loja"].lower(), extrair_generico)

    html, erro = baixar(oferta["url"])
    if erro:
        return {"data": agora, "erro": erro}

    if os.environ.get("DEBUG") == "1":
        diagnostico(rotulo, html)

    dados = extrator(html)
    if not dados.get("preco_atual"):
        return {"data": agora, "erro": "preço não encontrado na página"}

    return {"data": agora, **dados}


# ---------- Telegram ----------

def enviar_telegram(texto):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("Telegram não configurado (faltam os Secrets). Mensagem:\n" + texto)
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": texto, "disable_web_page_preview": "true"},
            timeout=30,
        )
        if r.status_code != 200:
            print(f"Telegram recusou a mensagem (HTTP {r.status_code}).")
    except requests.RequestException as e:
        # não imprimimos o erro completo: ele pode conter o endereço com o token
        print(f"Falha ao enviar ao Telegram ({type(e).__name__}).")


# ---------- resumo ----------

def linha_oferta(oferta, reg):
    if reg.get("erro"):
        return f"  {oferta['loja']}: ⚠️ sem leitura ({reg['erro']})"
    if reg.get("suspeita"):
        return f"  {oferta['loja']}: {formatar(reg['preco_atual'])} (a confirmar)"
    partes = [f"  {oferta['loja']}: {formatar(reg['preco_atual'])}"]
    if reg.get("preco_pix"):
        partes.append(f"Pix {formatar(reg['preco_pix'])}")
    if reg.get("parcelas_sem_juros"):
        partes.append(f"{reg['parcelas_sem_juros']}x de {formatar(reg['valor_parcela'])} s/ juros")
    return " · ".join(partes)


# ---------- principal ----------

def carregar_produtos():
    dados = json.loads(ARQ_PRODUTOS.read_text(encoding="utf-8"))
    cfg = {**PADROES, **dados.get("configuracoes", {})}
    return cfg, dados["produtos"]


def migrar_historico(historico, produtos):
    """Formato antigo (chave = id do produto) -> novo (chave = produto@loja)."""
    for p in produtos:
        ofertas = p["ofertas"]
        if p["id"] in historico and len(ofertas) == 1:
            nova = chave_oferta(p, ofertas[0])
            if nova not in historico:
                historico[nova] = historico.pop(p["id"])


def main():
    cfg, produtos = carregar_produtos()
    historico = {}
    if ARQ_HISTORICO.exists():
        historico = json.loads(ARQ_HISTORICO.read_text(encoding="utf-8"))
    migrar_historico(historico, produtos)

    primeira_vez = not historico
    pedir_resumo = os.environ.get("ENVIAR_RESUMO") == "1" or primeira_vez
    avisos, blocos = [], []

    for p in produtos:
        pct = float(p.get("queda_minima_pct", cfg["queda_minima_pct"]))
        limite_susp = float(cfg["variacao_suspeita_pct"])
        linhas, melhor = [], None

        for oferta in p["ofertas"]:
            chave = chave_oferta(p, oferta)
            hist = historico.setdefault(chave, [])
            nome = f"{p['nome']} ({oferta['loja']})"
            ultimo_foi_erro = bool(hist) and bool(hist[-1].get("erro"))

            reg = coletar(oferta, chave)
            anteriores = validos(hist)  # antes de qualquer reclassificação
            nova_susp, confirmada = classificar_leitura(hist, reg, limite_susp)
            hist.append(reg)
            linhas.append(linha_oferta(oferta, reg))

            if reg.get("erro"):
                print(f"[{chave}] erro: {reg['erro']}")
                if not ultimo_foi_erro:
                    avisos.append(
                        f"⚠️ Não consegui ler o preço de {nome} ({reg['erro']}).\n{oferta['url']}"
                    )
                continue

            print(f"[{chave}] {formatar(reg['preco_atual'])}"
                  + (" (suspeita)" if nova_susp else "")
                  + (" (confirmou a anterior)" if confirmada else ""))

            if nova_susp:
                ref = melhor_preco(anteriores[-1])
                avisos.append(
                    f"👀 Leitura fora do padrão: {nome}\n"
                    f"{formatar(ref)} → {formatar(melhor_preco(reg))}\n"
                    f"Vou confirmar na próxima coleta antes de contar no histórico.\n{oferta['url']}"
                )
                continue

            avisos += avaliar_avisos(nome, oferta["url"], anteriores, reg, pct, p.get("preco_alvo"))

            preco = melhor_preco(reg)
            if melhor is None or preco < melhor[0]:
                melhor = (preco, oferta["loja"])

        cabeca = f"• {p['nome']}"
        if len(p["ofertas"]) > 1 and melhor:
            cabeca += f"\n  🏅 Menor preço hoje: {formatar(melhor[0])} ({melhor[1]})"
        blocos.append(cabeca + "\n" + "\n".join(linhas))

    ARQ_HISTORICO.parent.mkdir(exist_ok=True)
    ARQ_HISTORICO.write_text(
        json.dumps(historico, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    if pedir_resumo:
        enviar_telegram("🛒 Rastreador de preços\n\n" + "\n\n".join(blocos))
    for aviso in avisos:
        enviar_telegram(aviso)


if __name__ == "__main__":
    sys.exit(main())
