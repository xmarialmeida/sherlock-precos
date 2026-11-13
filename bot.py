#!/usr/bin/env python3
"""Bot do Telegram: adiciona produtos ao rastreador a partir de links.

Fluxo:
  1. Você manda um link para o bot.
  2. O bot abre a página, lê loja, nome e preço, e pergunta (com botões) se é
     o mesmo produto de algum que você já acompanha ou se é um produto novo.
  3. Ao tocar num botão, o produto/oferta é gravado em products.json e a
     primeira leitura entra no histórico.

Como ele é acionado:
  - Modo imediato (recomendado): um "porteiro" na Cloudflare (worker.js) recebe o
    aviso do Telegram e aciona este script na hora, entregando a mensagem na
    variável ATUALIZACAO.
  - Modo antigo (reserva): sem ATUALIZACAO, o script pergunta ao Telegram se há
    mensagens novas (só funciona se o modo imediato estiver desligado).

Só obedece ao chat definido em TELEGRAM_CHAT_ID.
"""
import hashlib
import json
import os
import re
import sys
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

import scraper as s

ARQ_ESTADO = s.RAIZ / "data" / "bot_state.json"

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
    "ref", "ref_", "tag", "psc", "sr", "keywords", "qid", "forceinstallment",
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
        if k.lower() not in PARAMS_RASTREIO and not k.lower().startswith(PREFIXOS_RASTREIO)
    ]
    return urlunparse((p.scheme, p.netloc, p.path, "", urlencode(mantidos), ""))


def extrair_nome(html):
    soup = BeautifulSoup(html, "html.parser")
    meta = soup.find("meta", attrs={"property": "og:title"})
    titulo = (meta.get("content") if meta and meta.get("content") else "") or (
        soup.title.get_text() if soup.title else ""
    )
    titulo = re.split(r"\s+\|\s+", " ".join(titulo.split()))[0].strip()
    return (titulo or "Produto novo")[:120]


def curto(texto, n=42):
    return texto if len(texto) <= n else texto[: n - 1] + "…"


def marca(produto_id):
    """Código curto do produto para caber no botão do Telegram (limite de 64 caracteres)."""
    return hashlib.sha1(produto_id.encode()).hexdigest()[:12]


def id_unico(base, existentes):
    base = s.slug(base)[:60] or "produto"
    cand, i = base, 2
    while cand in existentes:
        cand, i = f"{base}-{i}", i + 1
    return cand


def todas_as_urls(produtos):
    return {o["url"] for p in produtos for o in p["ofertas"]}


def ler_oferta(url, loja):
    """Abre a página e devolve (html, dados, erro)."""
    html, erro = s.baixar(url)
    if erro:
        return None, {}, erro
    dados = s.EXTRATORES.get(loja.lower(), s.extrair_generico)(html)
    return html, dados, None


# ---------- ações ----------

def iniciar_adicao(chat_id, url_bruta, produtos):
    url = limpar_url(url_bruta)
    loja = s.loja_por_url(url)
    if not loja:
        responder(chat_id, "Esse link não parece válido. Mande o endereço completo, começando com https://")
        return
    if url in todas_as_urls(produtos):
        responder(chat_id, "Esse link eu já acompanho. 👍")
        return

    html, dados, erro = ler_oferta(url, loja)
    if erro:
        responder(chat_id, f"Não consegui abrir esse link de {loja} ({erro}). "
                           "A loja pode estar bloqueando o robô.")
        return
    if not dados.get("preco_atual"):
        resumo = "\n".join(s.inspecionar(html, max_valores=6))
        responder(
            chat_id,
            f"Abri a página de {loja}, mas não consegui ler o preço com segurança "
            "(não vou chutar um valor). Isso me ajuda a aprender essa loja:\n\n" + resumo[:3000],
        )
        return

    nome = extrair_nome(html)
    teclado = [[{"text": f"🔗 Mesmo que: {curto(p['nome'])}", "callback_data": f"j:{marca(p['id'])}"}]
               for p in produtos]
    teclado.append([{"text": "➕ É um produto novo", "callback_data": "n"}])
    teclado.append([{"text": "✖ Cancelar", "callback_data": "c"}])

    # O link fica escrito na própria mensagem: é dela que o bot lê ao tocar no botão,
    # então não é preciso guardar nada entre uma execução e outra.
    responder(
        chat_id,
        f"Encontrei:\n{nome}\nLoja: {loja} · {s.formatar(s.melhor_preco(dados))}\nLink: {url}\n\n"
        "Esse link é o MESMO produto (mesma versão e voltagem) de algum que você já acompanha, "
        "ou é um produto novo?",
        teclado,
    )


def concluir(chat_id, msg_id, acao_bruta, texto_msg, produtos, historico, cb_id=None):
    """Trata o toque num botão. Devolve True se a lista de produtos mudou."""
    def finalizar(texto):
        if cb_id:
            api("answerCallbackQuery", callback_query_id=cb_id)
        api("editMessageText", chat_id=chat_id, message_id=msg_id, text=texto,
            disable_web_page_preview=True, reply_markup={"inline_keyboard": []})

    partes = acao_bruta.split(":")
    acao = partes[0]
    if acao == "c":
        finalizar("Cancelado. Nada foi adicionado.")
        return False

    m_url = re.search(r"^Link: (\S+)$", texto_msg or "", re.M)
    linhas = (texto_msg or "").split("\n")
    if not m_url or len(linhas) < 2 or len(partes) > 2:
        finalizar("Essa pergunta é de uma versão antiga do bot. Mande o link de novo, por favor.")
        return False
    url, nome = m_url.group(1), linhas[1].strip()[:120]
    loja = s.loja_por_url(url)

    if url in todas_as_urls(produtos):
        finalizar(f"✅ “{nome}” ({loja}) já está na sua lista.")
        return False

    oferta = {"loja": loja, "url": url}
    if acao == "j":
        prod = next((p for p in produtos if len(partes) == 2 and marca(p["id"]) == partes[1]), None)
        if not prod:
            finalizar("Não encontrei esse produto na lista. Mande o link de novo.")
            return False
        if any(o["loja"] == loja for o in prod["ofertas"]):
            oferta["id"] = f"{prod['id']}@{s.slug(loja)}-{len(prod['ofertas']) + 1}"
        prod["ofertas"].append(oferta)
        resumo = f"✅ Adicionei ao produto “{prod['nome']}” como mais uma oferta ({loja})."
    elif acao == "n":
        prod = {
            "id": id_unico(nome, {p["id"] for p in produtos}),
            "nome": nome, "preco_alvo": None, "ofertas": [oferta],
        }
        produtos.append(prod)
        resumo = f"✅ Novo produto adicionado: “{nome}” ({loja})."
    else:
        finalizar("Não entendi esse botão. Mande o link de novo.")
        return False

    _, dados, _ = ler_oferta(url, loja)
    if dados.get("preco_atual"):
        leitura = {"data": datetime.now(s.FUSO).isoformat(timespec="minutes"), **dados}
        historico.setdefault(s.chave_oferta(prod, oferta), []).append(leitura)
        primeira = f"Primeira leitura: {s.formatar(s.melhor_preco(leitura))}."
    else:
        primeira = "A primeira leitura entra na próxima coleta."
    finalizar(f"{resumo}\n{primeira} A partir de agora eu acompanho e aviso quando houver queda relevante.")
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


def tratar_mensagem(chat_id, texto, produtos, historico):
    links = re.findall(r"https?://\S+", texto)
    if texto.startswith("/lista"):
        listar(chat_id, produtos, historico)
    elif links:
        for link in links:
            iniciar_adicao(chat_id, link, produtos)
    else:
        responder(chat_id, AJUDA)


# ---------- principal ----------

def main():
    autorizado = str(os.environ.get("TELEGRAM_CHAT_ID", ""))
    dados_prod = json.loads(s.ARQ_PRODUTOS.read_text(encoding="utf-8"))
    produtos = dados_prod["produtos"]
    historico = json.loads(s.ARQ_HISTORICO.read_text(encoding="utf-8")) if s.ARQ_HISTORICO.exists() else {}
    mudou = False

    bruto = os.environ.get("ATUALIZACAO", "").strip()
    if bruto:
        # Modo imediato: o porteiro (worker.js) já conferiu que a mensagem é sua.
        at = json.loads(bruto)
        print(f"Modo imediato: {at.get('tipo')}")
        if at.get("tipo") == "botao":
            mudou = concluir(autorizado, at["msg_id"], at.get("acao", ""), at.get("texto", ""),
                             produtos, historico)
        else:
            tratar_mensagem(autorizado, at.get("texto", "") or "", produtos, historico)
    else:
        # Modo antigo: pergunta ao Telegram se há mensagens novas.
        estado = {"offset": 0}
        if ARQ_ESTADO.exists():
            estado["offset"] = json.loads(ARQ_ESTADO.read_text(encoding="utf-8")).get("offset", 0)
        pedido = {"timeout": 0, "allowed_updates": ["message", "callback_query"]}
        if estado["offset"]:
            pedido["offset"] = estado["offset"] + 1
        resp = api("getUpdates", **pedido)
        atualizacoes = resp.get("result", []) if resp.get("ok") else []
        print(f"{len(atualizacoes)} atualização(ões) recebida(s).")
        for up in atualizacoes:
            estado["offset"] = max(estado["offset"], up["update_id"])
            msg, cb = up.get("message"), up.get("callback_query")
            if cb:
                if str(cb["message"]["chat"]["id"]) == autorizado:
                    mudou = concluir(autorizado, cb["message"]["message_id"], cb.get("data", ""),
                                     cb["message"].get("text", ""), produtos, historico, cb["id"]) or mudou
            elif msg and str(msg["chat"]["id"]) == autorizado:
                tratar_mensagem(autorizado, msg.get("text", "") or "", produtos, historico)
        if atualizacoes:
            ARQ_ESTADO.parent.mkdir(exist_ok=True)
            ARQ_ESTADO.write_text(json.dumps(estado, ensure_ascii=False, indent=1), encoding="utf-8")

    if mudou:
        dados_prod["produtos"] = produtos
        s.ARQ_PRODUTOS.write_text(json.dumps(dados_prod, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        s.ARQ_HISTORICO.parent.mkdir(exist_ok=True)
        s.ARQ_HISTORICO.write_text(json.dumps(historico, ensure_ascii=False, indent=1), encoding="utf-8")
        print("Lista de produtos atualizada.")


if __name__ == "__main__":
    sys.exit(main())
