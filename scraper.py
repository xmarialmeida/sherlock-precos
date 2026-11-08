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
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
    return re.sub(r"[^a-z0-9]+", "-", texto.lower()).strip("-")


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


def diagnostico(rotulo, html):
    """Mostra no log o que o robô enxergou. Só roda com DEBUG=1 (execução manual)."""
    soup = BeautifulSoup(html, "html.parser")
    texto = soup.get_text(" ", strip=True)
    print(f"\n===== DIAGNÓSTICO: {rotulo} =====")
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


def coletar(oferta, rotulo):
    agora = datetime.now(FUSO).isoformat(timespec="minutes")
    extrator = EXTRATORES.get(oferta["loja"].lower())
    if not extrator:
        return {"data": agora, "erro": f"loja '{oferta['loja']}' ainda não suportada"}

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
