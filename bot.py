#!/usr/bin/env python3
"""Bot do Telegram: adiciona produtos ao rastreador a partir de links.

Fluxo:
  1. Você manda um link para o bot.
  2. O bot abre a página, lê loja, nome e preço, e pergunta (com botões) se é
     o mesmo produto de algum que você já acompanha ou se é um produto novo.
  3. Ao tocar num botão, o produto/oferta é gravado em products.json e a
     primeira leitura entra no histórico.

Roda a cada ~15 minutos pelo GitHub Actions (não precisa de servidor).
Só obedece ao chat definido em TELEGRAM_CHAT_ID; mensagens de outros são ignoradas.
"""
import json
import os
import re
import secrets
import sys
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

import scraper as s

ARQ_ESTADO = s.RAIZ / "data" / "bot_state.json"
VALIDADE_PERGUNTA = timedelta(days=3)

AJUDA = (
    "👋 Eu acompanho preços para você.\n\n"
    "• Me mande o link de um produto e eu pergunto se ele é novo ou a mesma "
    "coisa que algo que você já acompanha em outra loja.\n"
    "• /lista mostra o que você acompanha.\n\n"
    "Tento ler qualquer loja. Se a página não mostrar o preço de forma que o robô entenda, "
    "eu te aviso e mando um resumo do que vi, para eu aprender a ler aquela loja."
)

# parâmetros que só servem para rastrear cliques (podem ser removidos sem mudar o produto)
PARAMS_RASTREIO = {
    "gclid", "fbclid", "msclkid", "srsltid", "dclid", "yclid", "mc_cid", "mc_eid",
    "ref", "ref_", "tag", "psc", "sr", "keywords", "qid", "pd_rd_r", "pd_rd_w", "pd_rd_wg",
    "pf_rd_r", "pf_rd_p", "matt_tool", "matt_word", "matt_source", "matt_campaign",
    "matt_adgroup", "matt_match_type", "matt_ad_type", "forceInstallment",
}
PREFIXOS_RASTREIO = ("utm_", "matt_", "gad_", "pd_rd_", "pf_rd_", "tracking", "wid", "polycard")


# ---------- Telegram ----------

def api(metodo, **dados):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        print(f"[sem token] {metodo}: {json.dumps(dados, ensure_ascii=False)[:300]}")
        return {"ok": False}
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/{metodo}", json=dados, timeout=30)
        resp = r.json()
        if not resp.get("ok"):
            print(f"Telegram recusou {metodo}: {resp.get('description')}")
        return resp
    except (requests.RequestException, ValueError) as e:
        # não imprimimos o erro completo: ele pode conter o endereço com o token
        print(f"Falha em {metodo} ({type(e).__name__}).")
        return {"ok": False}


def responder(chat_id, texto, teclado=None):
    dados = {"chat_id": chat_id, "text": texto, "disable_web_page_preview": True}
    if teclado:
        dados["reply_markup"] = {"inline_keyboard": teclado}
    return api("sendMessage", **dados)


# ---------- utilitários ----------

def limpar_url(url):
    """Remove só o rastreamento do link, mantendo o que identifica o produto/vendedor
    (ex.: seller_id, idsku, IdSku, idLojista). Também tira pontuação colada ao link."""
    url = url.rstrip(".,;:)>]}\"'")
    p = urlparse(url)
    mantidos = [
        (k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
        if k.lower() not in {x.lower() for x in PARAMS_RASTREIO}
        and not k.lower().startswith(PREFIXOS_RASTREIO)
    ]
    return urlunparse((p.scheme, p.netloc, p.path, "", urlencode(mantidos), ""))


def extrair_nome(html):
    soup = BeautifulSoup(html, "html.parser")
    meta = soup.find("meta", attrs={"property": "og:title"})
    titulo = (meta.get("content") if meta and meta.get("content") else "") or (
        soup.title.get_text() if soup.title else ""
    )
    titulo = re.split(r"\s+\|\s+", titulo.strip())[0].strip()
    return (titulo or "Produto novo")[:120]


def curto(texto, n=42):
    return texto if len(texto) <= n else texto[: n - 1] + "…"


def id_unico(base, existentes):
    base = s.slug(base)[:60] or "produto"
    cand, i = base, 2
    while cand in existentes:
        cand, i = f"{base}-{i}", i + 1
    return cand


def todas_as_urls(produtos):
    return {o["url"] for p in produtos for o in p["ofertas"]}


# ---------- ações ----------

def iniciar_adicao(chat_id, url_bruta, produtos, estado):
    url = limpar_url(url_bruta)
    loja = s.loja_por_url(url)
    if not loja:
        responder(chat_id, "Esse link não parece válido. Mande o endereço completo, começando com https://")
        return
    if url in todas_as_urls(produtos):
        responder(chat_id, "Esse link eu já acompanho. 👍")
        return

    html, erro = s.baixar(url)
    if erro:
        responder(chat_id, f"Não consegui abrir esse link ({erro}). Tente de novo mais tarde.")
        return
    dados = s.EXTRATORES.get(loja.lower(), s.extrair_generico)(html)
    if not dados.get("preco_atual"):
        resumo = "\n".join(s.inspecionar(html, max_valores=6))
        responder(
            chat_id,
            f"Abri a página de {loja}, mas não consegui ler o preço com segurança "
            "(não vou chutar um valor). Isso me ajuda a aprender essa loja:\n\n" + resumo[:3000],
        )
        return

    nome = extrair_nome(html)
    leitura = {"data": datetime.now(s.FUSO).isoformat(timespec="minutes"), **dados}
    pid = secrets.token_hex(3)
    estado["pendentes"][pid] = {
        "url": url, "loja": loja, "nome": nome, "leitura": leitura,
        "ids": [p["id"] for p in produtos], "criado": leitura["data"],
    }

    teclado = [[{"text": f"🔗 Mesmo que: {curto(p['nome'])}", "callback_data": f"j:{pid}:{i}"}]
               for i, p in enumerate(produtos)]
    teclado.append([{"text": "➕ É um produto novo", "callback_data": f"n:{pid}"}])
    teclado.append([{"text": "✖ Cancelar", "callback_data": f"c:{pid}"}])

    preco = s.formatar(s.melhor_preco(leitura))
    responder(
        chat_id,
        f"Encontrei:\n{nome}\nLoja: {loja} · {preco}\n\n"
        "Esse link é o MESMO produto (mesma versão e voltagem) de algum que você já acompanha, "
        "ou é um produto novo?",
        teclado,
    )


def concluir(cb, estado, produtos, historico):
    dados = cb["data"].split(":")
    acao, pid = dados[0], dados[1]
    chat_id = cb["message"]["chat"]["id"]
    msg_id = cb["message"]["message_id"]
    pend = estado["pendentes"].get(pid)

    def finalizar(texto, aviso=None):
        api("answerCallbackQuery", callback_query_id=cb["id"], text=aviso or "")
        api("editMessageText", chat_id=chat_id, message_id=msg_id, text=texto,
            disable_web_page_preview=True, reply_markup={"inline_keyboard": []})

    if not pend:
        finalizar("Essa pergunta já foi respondida ou expirou. Mande o link de novo se quiser.", "Expirou")
        return False

    if acao == "c":
        estado["pendentes"].pop(pid)
        finalizar("Cancelado. Nada foi adicionado.")
        return False

    oferta = {"loja": pend["loja"], "url": pend["url"]}
    if acao == "j":
        idx = int(dados[2])
        prod = next((p for p in produtos if p["id"] == pend["ids"][idx]), None)
        if not prod:
            finalizar("Não encontrei esse produto na lista. Mande o link de novo.")
            estado["pendentes"].pop(pid)
            return False
        if any(o["loja"] == pend["loja"] for o in prod["ofertas"]):
            oferta["id"] = f"{prod['id']}@{s.slug(pend['loja'])}-{len(prod['ofertas']) + 1}"
        prod["ofertas"].append(oferta)
        resumo = f"✅ Adicionei ao produto “{prod['nome']}” como mais uma oferta ({pend['loja']})."
    else:  # "n": produto novo
        prod = {
            "id": id_unico(pend["nome"], {p["id"] for p in produtos}),
            "nome": pend["nome"], "preco_alvo": None, "ofertas": [oferta],
        }
        produtos.append(prod)
        resumo = f"✅ Novo produto adicionado: “{prod['nome']}” ({pend['loja']})."

    historico.setdefault(s.chave_oferta(prod, oferta), []).append(pend["leitura"])
    estado["pendentes"].pop(pid)
    finalizar(f"{resumo}\nPrimeira leitura: {s.formatar(s.melhor_preco(pend['leitura']))}. "
              "A partir de agora eu acompanho e aviso quando houver queda relevante.")
    return True


def listar(chat_id, produtos, historico):
    if not produtos:
        responder(chat_id, "Você ainda não acompanha nenhum produto. Me mande um link!")
        return
    blocos = []
    for p in produtos:
        linhas = [f"• {p['nome']}"]
        for o in p["ofertas"]:
            hist = s.validos(historico.get(s.chave_oferta(p, o), []))
            valor = s.formatar(s.melhor_preco(hist[-1])) if hist else "sem leitura ainda"
            linhas.append(f"   {o['loja']}: {valor}")
        blocos.append("\n".join(linhas))
    responder(chat_id, "Você acompanha:\n\n" + "\n\n".join(blocos))


# ---------- principal ----------

def main():
    autorizado = str(os.environ.get("TELEGRAM_CHAT_ID", ""))
    dados_prod = json.loads(s.ARQ_PRODUTOS.read_text(encoding="utf-8"))
    produtos = dados_prod["produtos"]
    historico = json.loads(s.ARQ_HISTORICO.read_text(encoding="utf-8")) if s.ARQ_HISTORICO.exists() else {}
    estado = {"offset": 0, "pendentes": {}}
    if ARQ_ESTADO.exists():
        estado.update(json.loads(ARQ_ESTADO.read_text(encoding="utf-8")))

    # descarta perguntas antigas
    limite = datetime.now(s.FUSO) - VALIDADE_PERGUNTA
    estado["pendentes"] = {
        k: v for k, v in estado["pendentes"].items() if datetime.fromisoformat(v["criado"]) > limite
    }

    pedido = {"timeout": 0, "allowed_updates": ["message", "callback_query"]}
    if estado["offset"]:
        pedido["offset"] = estado["offset"] + 1
    resp = api("getUpdates", **pedido)
    atualizacoes = resp.get("result", []) if resp.get("ok") else []
    print(f"{len(atualizacoes)} atualização(ões) recebida(s).")

    mudou = False
    for up in atualizacoes:
        estado["offset"] = max(estado["offset"], up["update_id"])
        msg, cb = up.get("message"), up.get("callback_query")

        if cb:
            if str(cb["message"]["chat"]["id"]) != autorizado:
                continue
            mudou = concluir(cb, estado, produtos, historico) or mudou
            continue
        if not msg or str(msg["chat"]["id"]) != autorizado:
            continue  # ignora quem não é você

        texto = msg.get("text", "") or ""
        chat_id = msg["chat"]["id"]
        links = re.findall(r"https?://\S+", texto)
        if texto.startswith("/lista"):
            listar(chat_id, produtos, historico)
        elif links:
            for link in links:
                iniciar_adicao(chat_id, link, produtos, estado)
        else:
            responder(chat_id, AJUDA)

    dados_prod["produtos"] = produtos
    s.ARQ_PRODUTOS.write_text(json.dumps(dados_prod, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    s.ARQ_HISTORICO.parent.mkdir(exist_ok=True)
    s.ARQ_HISTORICO.write_text(json.dumps(historico, ensure_ascii=False, indent=1), encoding="utf-8")
    ARQ_ESTADO.write_text(json.dumps(estado, ensure_ascii=False, indent=1), encoding="utf-8")
    if mudou:
        print("Lista de produtos atualizada.")


if __name__ == "__main__":
    sys.exit(main())
