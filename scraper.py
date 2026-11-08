#!/usr/bin/env python3
"""Rastreador de preços.

Lê products.json, consulta cada página, guarda o histórico em data/history.json
e avisa no Telegram quando algo relevante acontece.
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

RAIZ = Path(__file__).parent
ARQ_PRODUTOS = RAIZ / "products.json"
ARQ_HISTORICO = RAIZ / "data" / "history.json"
FUSO = timezone(timedelta(hours=-3))  # horário de Brasília

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


def melhor_preco(reg):
    """Menor valor pago hoje: o Pix, se existir e for menor, senão o preço atual."""
    candidatos = [v for v in (reg.get("preco_atual"), reg.get("preco_pix")) if v]
    return min(candidatos) if candidatos else None


# ---------- leitura das páginas ----------

def extrair_brastemp(html):
    """Lê preço, Pix e parcelamento de uma página da loja Brastemp."""
    soup = BeautifulSoup(html, "html.parser")
    texto = soup.get_text(" ", strip=True)
    dados = {}

    meta = (
        soup.find("meta", attrs={"property": "product:price:amount"})
        or soup.find("meta", attrs={"name": "product:price:amount"})
    )
    if meta and meta.get("content"):
        try:
            dados["preco_atual"] = float(meta["content"])
        except ValueError:
            pass

    m = re.search(PRECO + r"\s*(?:\S+\s*){0,3}Oferta", texto)
    if m:
        dados["preco_original"] = brl_para_float(m.group(1))

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

    return dados


EXTRATORES = {"brastemp": extrair_brastemp}


def baixar(url):
    ultimo_erro = "falha desconhecida"
    for tentativa in range(3):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            if r.status_code == 200:
                return r.text, None
            ultimo_erro = f"HTTP {r.status_code}"
        except requests.RequestException as e:
            ultimo_erro = type(e).__name__
        time.sleep(3 * (tentativa + 1))
    return None, ultimo_erro


def diagnostico(produto, html):
    """Mostra no log o que o robô enxergou. Só roda com DEBUG=1 (execução manual)."""
    soup = BeautifulSoup(html, "html.parser")
    texto = soup.get_text(" ", strip=True)
    print(f"\n===== DIAGNÓSTICO: {produto['id']} =====")
    for chave in ("property", "name"):
        meta = soup.find("meta", attrs={chave: "product:price:amount"})
        if meta:
            print(f"meta product:price:amount = {meta.get('content')}")
            break
    else:
        print("meta product:price:amount: NÃO ENCONTRADA")
    print("Parcelamentos encontrados no texto:")
    for m in re.finditer(r"\d+\s*x\s*de\s*R\$\s*" + PRECO + r"[^.]{0,20}", texto, re.I):
        print("  -", m.group(0).strip())
    print("Primeiros valores 'R$' encontrados (com contexto):")
    for m in list(re.finditer(r"R\$\s*" + PRECO, texto))[:12]:
        ini = max(0, m.start() - 30)
        print("  -", texto[ini : m.end() + 25].replace("\n", " "))
    try:
        pais = requests.get("https://ipinfo.io/country", timeout=15).text.strip()
        print(f"País de onde o robô está acessando: {pais}")
    except requests.RequestException:
        print("País de onde o robô está acessando: não consegui descobrir")
    print("=====\n")


def coletar(produto):
    agora = datetime.now(FUSO).isoformat(timespec="minutes")
    extrator = EXTRATORES.get(produto["loja"].lower())
    if not extrator:
        return {"data": agora, "erro": f"loja '{produto['loja']}' ainda não suportada"}

    html, erro = baixar(produto["url"])
    if erro:
        return {"data": agora, "erro": erro}

    if os.environ.get("DEBUG") == "1":
        diagnostico(produto, html)

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


# ---------- lógica principal ----------

def validos(hist):
    return [r for r in hist if not r.get("erro")]


def linha_resumo(produto, reg):
    if reg.get("erro"):
        return f"• {produto['nome']}\n  ⚠️ sem leitura ({reg['erro']})"
    partes = [f"• {produto['nome']}", f"  Preço: {formatar(reg['preco_atual'])}"]
    if reg.get("preco_pix"):
        partes.append(f"  Pix: {formatar(reg['preco_pix'])}")
    if reg.get("parcelas_sem_juros"):
        partes.append(
            f"  {reg['parcelas_sem_juros']}x de {formatar(reg['valor_parcela'])} sem juros"
        )
    return "\n".join(partes)


def main():
    produtos = json.loads(ARQ_PRODUTOS.read_text(encoding="utf-8"))
    historico = {}
    if ARQ_HISTORICO.exists():
        historico = json.loads(ARQ_HISTORICO.read_text(encoding="utf-8"))

    primeira_vez = not historico
    pedir_resumo = os.environ.get("ENVIAR_RESUMO") == "1" or primeira_vez
    avisos, linhas = [], []

    for p in produtos:
        hist = historico.setdefault(p["id"], [])
        anteriores = validos(hist)
        ultimo_foi_erro = bool(hist) and bool(hist[-1].get("erro"))

        reg = coletar(p)
        hist.append(reg)
        linhas.append(linha_resumo(p, reg))

        if reg.get("erro"):
            print(f"[{p['id']}] erro: {reg['erro']}")
            if not ultimo_foi_erro:
                avisos.append(
                    f"⚠️ Não consegui ler o preço de {p['nome']} ({reg['erro']}).\n{p['url']}"
                )
            continue

        print(f"[{p['id']}] {formatar(reg['preco_atual'])}")
        if not anteriores:
            continue

        antes = anteriores[-1]
        agora_melhor, antes_melhor = melhor_preco(reg), melhor_preco(antes)

        if reg["preco_atual"] < antes["preco_atual"]:
            avisos.append(
                f"📉 Baixou: {p['nome']}\n"
                f"{formatar(antes['preco_atual'])} → {formatar(reg['preco_atual'])}\n{p['url']}"
            )

        minimo_anterior = min(melhor_preco(r) for r in anteriores)
        if len(anteriores) >= 2 and agora_melhor < minimo_anterior:
            avisos.append(
                f"🏆 Menor preço registrado: {p['nome']}\n"
                f"{formatar(agora_melhor)} (antes: {formatar(minimo_anterior)})\n{p['url']}"
            )

        alvo = p.get("preco_alvo")
        if alvo and agora_melhor <= alvo < antes_melhor:
            avisos.append(
                f"🎯 Bateu sua meta de {formatar(alvo)}: {p['nome']}\n"
                f"Agora: {formatar(agora_melhor)}\n{p['url']}"
            )

    ARQ_HISTORICO.parent.mkdir(exist_ok=True)
    ARQ_HISTORICO.write_text(
        json.dumps(historico, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    if pedir_resumo:
        enviar_telegram("🛒 Rastreador de preços ativo!\n\n" + "\n\n".join(linhas))
    for aviso in avisos:
        enviar_telegram(aviso)


if __name__ == "__main__":
    sys.exit(main())
