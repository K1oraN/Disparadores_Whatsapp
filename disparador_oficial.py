#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DISPARADOR OFICIAL - WhatsApp Cloud API (Meta)

Comandos:
  python disparador_oficial.py importar            le o contatos.csv e monta a fila da campanha
  python disparador_oficial.py simular             mostra dias, tempo por dia e custo estimado
  python disparador_oficial.py teste 5582999999999 [Nome]   envia 1 mensagem de teste
  python disparador_oficial.py enviar              dispara respeitando os limites (Ctrl+C pausa)
  python disparador_oficial.py webhook             recebe entregas, falhas e pedidos de SAIR
  python disparador_oficial.py status              resumo da campanha
  python disparador_oficial.py sair 5582999999999  tira a pessoa da lista de promocoes
  python disparador_oficial.py voltar 5582999999999  devolve a pessoa para a lista
  python disparador_oficial.py reenfileirar [verificar]  devolve 'falhou' (ou 'verificar') para a fila
  python disparador_oficial.py liberar             destrava o disparo depois de um alerta grave
  python disparador_oficial.py relatorio           gera relatorio_<campanha>_oficial.csv

"SAIR" NAO bloqueia o numero no WhatsApp. So tira a pessoa das PROMOCOES deste
script. Mensagens manuais (exames, avisos) continuam funcionando normalmente.

Dependencias: pip install requests flask
"""
import csv
import hashlib
import hmac
import json
import math
import os
import random
import re
import sqlite3
import sys
import time
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE = Path(__file__).resolve().parent
CANAL = "whatsapp_api"


# =====================================================================
# Configuracao (.env)
# =====================================================================
def carregar_env():
    arq = BASE / ".env"
    if not arq.exists():
        return
    for linha in arq.read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("#") or "=" not in linha:
            continue
        k, v = linha.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


carregar_env()


def cfg(k, padrao=""):
    return os.environ.get(k, str(padrao)).strip()


def cfg_int(k, padrao):
    return int(float(cfg(k, padrao)))


def cfg_float(k, padrao):
    return float(cfg(k, padrao).replace(",", "."))


def cfg_bool(k, padrao):
    return cfg(k, padrao).lower() in ("1", "true", "sim", "s", "yes")


# Geral
CAMPANHA = cfg("CAMPANHA", "campanha_1")
ARQUIVO_CONTATOS = cfg("ARQUIVO_CONTATOS", "contatos.csv")
BANCO = cfg("BANCO", "disparos.db")
IMAGEM_LINK = cfg("IMAGEM_LINK")
HORA_INICIO = cfg_int("HORA_INICIO", 9)
HORA_FIM = cfg_int("HORA_FIM", 19)
ENVIAR_DOMINGO = cfg_bool("ENVIAR_DOMINGO", 0)
NOME_PADRAO = cfg("NOME_PADRAO", "cliente")
# Filtro de periodo (ex.: so clientes atendidos de 2024 para ca)
COLUNA_DATA = cfg("COLUNA_DATA", "ultimo_atendimento").lower()
DATA_MINIMA_CFG = cfg("DATA_MINIMA", "2024-01-01")
ANOS_RETROATIVOS = cfg_int("ANOS_RETROATIVOS", 0)
ACEITAR_SEM_DATA = cfg_bool("ACEITAR_SEM_DATA", 0)
ESPERA_LIMITADO_H = cfg_int("ESPERA_LIMITADO_H", 48)

# Oficial
WA_TOKEN = cfg("WA_TOKEN")
WA_PHONE_NUMBER_ID = cfg("WA_PHONE_NUMBER_ID")
WA_API_VERSION = cfg("WA_API_VERSION", "v25.0")
TEMPLATE_NOME = cfg("TEMPLATE_NOME")
TEMPLATE_IDIOMA = cfg("TEMPLATE_IDIOMA", "pt_BR")
TEMPLATE_USA_NOME = cfg_bool("TEMPLATE_USA_NOME", 1)
LIMITE_24H = cfg_int("LIMITE_24H", 200)
LOTE = cfg_int("LOTE", 20)
INTERVALO_MIN = cfg_float("INTERVALO_MIN", 8)
INTERVALO_MAX = cfg_float("INTERVALO_MAX", 20)
PAUSA_LOTE_MIN = cfg_float("PAUSA_LOTE_MIN", 240)
PAUSA_LOTE_MAX = cfg_float("PAUSA_LOTE_MAX", 420)
CUSTO_POR_MSG = cfg_float("CUSTO_POR_MSG", 0.3217)

# Webhook
WEBHOOK_VERIFY_TOKEN = cfg("WEBHOOK_VERIFY_TOKEN")
WA_APP_SECRET = cfg("WA_APP_SECRET")
WEBHOOK_PORTA = cfg_int("WEBHOOK_PORTA", 8000)
CONFIRMAR_SAIDA = cfg_bool("CONFIRMAR_SAIDA", 1)
MSG_CONFIRMA_SAIDA = cfg(
    "MSG_CONFIRMA_SAIDA",
    "Pronto! Voce nao vai mais receber nossas promocoes. "
    "Seus resultados e avisos continuam chegando normalmente.",
)

# Codigos de erro da Meta
ERROS_FATAIS = {
    131048: "Limite de spam: a Meta esta segurando seus envios. Pare e revise a qualidade.",
    132015: "Template PAUSADO pela Meta por baixa qualidade.",
    132016: "Template DESATIVADO pela Meta.",
    132000: "Quantidade de variaveis nao bate com o template (confira TEMPLATE_USA_NOME).",
    132001: "Template nao encontrado (confira TEMPLATE_NOME e TEMPLATE_IDIOMA).",
    132012: "Parametros nao batem com o template (ex.: imagem no cabecalho x IMAGEM_LINK).",
    190: "Token invalido ou expirado (use token permanente de System User).",
    368: "Conta bloqueada temporariamente por violacao de politica.",
    131031: "Conta do WhatsApp Business bloqueada ou restrita.",
    131042: "Problema com a forma de pagamento da conta.",
}
ERROS_ESPERAR = {4, 80007, 130429, 131056}  # velocidade: espera e tenta de novo
ERROS_LIMITADO = {131049}  # limite de marketing por pessoa: esperar 24h+
ERROS_OPTOUT = {131050}  # pessoa desligou promocoes da empresa no proprio WhatsApp


# =====================================================================
# Utilidades
# =====================================================================
def log(msg):
    print(f"[{datetime.now():%d/%m %H:%M:%S}] {msg}", flush=True)


def sair_erro(msg):
    print(f"ERRO: {msg}")
    sys.exit(1)


def agora():
    return datetime.now().isoformat(timespec="seconds")


def norm_texto(t):
    t = unicodedata.normalize("NFKD", t or "").encode("ascii", "ignore").decode()
    return " ".join(re.sub(r"[^A-Z ]", " ", t.upper()).split())


PALAVRAS_SAIR = {
    norm_texto(p)
    for p in cfg("PALAVRAS_SAIR", "SAIR,PARAR,STOP,CANCELAR,DESCADASTRAR,PARAR PROMOCOES").split(",")
    if p.strip()
}


def normalizar_tel(bruto):
    d = re.sub(r"\D", "", str(bruto or "")).lstrip("0")
    if len(d) in (10, 11):
        d = "55" + d
    return d if 12 <= len(d) <= 15 else None


def variantes(tel):
    """Celulares antigos no Brasil podem vir da Meta sem o 9 extra."""
    v = {tel}
    if tel.startswith("55"):
        if len(tel) == 13 and tel[4] == "9":
            v.add(tel[:4] + tel[5:])
        elif len(tel) == 12 and tel[4] in "6789":
            v.add(tel[:4] + "9" + tel[4:])
    return list(v)


def primeiro_nome(nome):
    return nome.split()[0].title() if nome and nome.strip() else NOME_PADRAO


def parse_data(bruto):
    """Aceita 15/03/2024, 15-03-2024, 15/03/24, 2024-03-15 (com ou sem hora). Devolve AAAA-MM-DD."""
    s = (bruto or "").strip().split(" ")[0].split("T")[0]
    if not s:
        return None
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d.%m.%Y", "%Y/%m/%d", "%d/%m/%y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def data_minima():
    """Data de corte do filtro de periodo (AAAA-MM-DD) ou None se nao houver filtro."""
    if DATA_MINIMA_CFG:
        d = parse_data(DATA_MINIMA_CFG)
        if not d:
            sair_erro(f"DATA_MINIMA invalida no .env: {DATA_MINIMA_CFG!r} (use 2024-01-01)")
        return d
    if ANOS_RETROATIVOS > 0:
        hoje = datetime.now().date()
        try:
            return hoje.replace(year=hoje.year - ANOS_RETROATIVOS).isoformat()
        except ValueError:  # 29/02
            return hoje.replace(year=hoje.year - ANOS_RETROATIVOS, day=28).isoformat()
    return None


def br(iso):
    return f"{iso[8:10]}/{iso[5:7]}/{iso[:4]}" if iso else ""


# =====================================================================
# Banco de dados (compartilhado entre o oficial e o nao oficial)
# =====================================================================
SCHEMA = """
CREATE TABLE IF NOT EXISTS contatos(
    telefone TEXT PRIMARY KEY, nome TEXT, consentimento TEXT, origem TEXT,
    optout INTEGER NOT NULL DEFAULT 0, optout_em TEXT, optout_motivo TEXT,
    data_ref TEXT);
CREATE TABLE IF NOT EXISTS envios(
    id INTEGER PRIMARY KEY AUTOINCREMENT, telefone TEXT NOT NULL, canal TEXT NOT NULL,
    campanha TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pendente', msg_id TEXT,
    erro TEXT, tentativas INTEGER NOT NULL DEFAULT 0, enviado_em TEXT, atualizado_em TEXT,
    UNIQUE(telefone, canal, campanha));
CREATE INDEX IF NOT EXISTS ix_envios_msg ON envios(msg_id);
CREATE INDEX IF NOT EXISTS ix_envios_data ON envios(canal, enviado_em);
CREATE TABLE IF NOT EXISTS controle(chave TEXT PRIMARY KEY, valor TEXT);
"""

SQL_PENDENTES = """
SELECT e.id, e.telefone, c.nome FROM envios e JOIN contatos c ON c.telefone = e.telefone
WHERE e.canal = ? AND e.campanha = ? AND c.optout = 0
  AND (? IS NULL OR c.data_ref >= ? OR (c.data_ref IS NULL AND ? = 1))
  AND (e.status = 'pendente'
       OR (e.status = 'limitado' AND e.tentativas < 2 AND e.atualizado_em <= ?))
  AND NOT EXISTS (SELECT 1 FROM envios o
                  WHERE o.telefone = e.telefone AND o.campanha = e.campanha AND o.canal <> e.canal
                    AND o.status IN ('enviando','verificar','enviado','entregue','lido','limitado'))
ORDER BY e.id LIMIT ?
"""


def conectar():
    con = sqlite3.connect(BASE / BANCO, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    # bancos criados antes do filtro de periodo ganham a coluna nova
    if "data_ref" not in {r[1] for r in con.execute("PRAGMA table_info(contatos)")}:
        con.execute("ALTER TABLE contatos ADD COLUMN data_ref TEXT")
    return con


def esta_fora(con, tel):
    vs = variantes(tel)
    q = ",".join("?" * len(vs))
    r = con.execute(f"SELECT MAX(optout) FROM contatos WHERE telefone IN ({q})", vs).fetchone()[0]
    return bool(r)


def marcar_optout(con, tel, motivo):
    vs = variantes(tel)
    q = ",".join("?" * len(vs))
    n = con.execute(
        f"UPDATE contatos SET optout=1, optout_em=?, optout_motivo=? WHERE telefone IN ({q})",
        (agora(), motivo, *vs),
    ).rowcount
    if not n:
        con.execute(
            "INSERT OR IGNORE INTO contatos(telefone, nome, optout, optout_em, optout_motivo) "
            "VALUES(?, '', 1, ?, ?)",
            (tel, agora(), motivo),
        )
    con.execute(
        f"UPDATE envios SET status='optout', atualizado_em=? "
        f"WHERE telefone IN ({q}) AND status IN ('pendente','limitado')",
        (agora(), *vs),
    )


def buscar_pendentes(con, limite):
    corte = (datetime.now() - timedelta(hours=ESPERA_LIMITADO_H)).isoformat(timespec="seconds")
    dm = data_minima()
    return con.execute(
        SQL_PENDENTES, (CANAL, CAMPANHA, dm, dm, int(ACEITAR_SEM_DATA), corte, limite)
    ).fetchall()


def enviados_24h(con):
    corte = (datetime.now() - timedelta(hours=24)).isoformat(timespec="seconds")
    return con.execute(
        "SELECT COUNT(DISTINCT telefone) FROM envios WHERE canal=? AND enviado_em >= ?", (CANAL, corte)
    ).fetchone()[0]


def parada_ativa(con):
    r = con.execute("SELECT valor FROM controle WHERE chave=?", (f"parar_{CANAL}",)).fetchone()
    return r[0] if r else None


def travar(con, motivo):
    con.execute(
        "INSERT OR REPLACE INTO controle(chave, valor) VALUES(?, ?)", (f"parar_{CANAL}", f"{agora()} {motivo}")
    )
    con.commit()


def ler_csv(caminho):
    texto = None
    for enc in ("utf-8-sig", "latin-1"):
        try:
            texto = Path(caminho).read_text(encoding=enc)
            break
        except UnicodeDecodeError:
            continue
    linhas = texto.splitlines() if texto else []
    if not linhas:
        return []
    sep = ";" if linhas[0].count(";") >= linhas[0].count(",") else ","
    return [
        {(k or "").strip().lower(): (v or "").strip() for k, v in linha.items()}
        for linha in csv.DictReader(linhas, delimiter=sep)
    ]


# =====================================================================
# Horario e esperas
# =====================================================================
def dentro_horario(dt=None):
    dt = dt or datetime.now()
    if dt.weekday() == 6 and not ENVIAR_DOMINGO:
        return False
    return HORA_INICIO <= dt.hour < HORA_FIM


def proximo_inicio():
    dt = datetime.now()
    cand = dt.replace(hour=HORA_INICIO, minute=0, second=0, microsecond=0)
    if dt >= cand:
        cand += timedelta(days=1)
    while not dentro_horario(cand):
        cand += timedelta(days=1)
    return cand


def dormir_ate(quando, motivo):
    seg = max(0, (quando - datetime.now()).total_seconds())
    log(f"{motivo}. Volto em {quando:%d/%m %H:%M} (deixe o programa aberto).")
    time.sleep(seg)


def esperar_janela_24h(con):
    corte = (datetime.now() - timedelta(hours=24)).isoformat(timespec="seconds")
    mais_antigo = con.execute(
        "SELECT MIN(enviado_em) FROM envios WHERE canal=? AND enviado_em >= ?", (CANAL, corte)
    ).fetchone()[0]
    libera = datetime.fromisoformat(mais_antigo) + timedelta(hours=24, minutes=1)
    dormir_ate(libera, f"Limite de {LIMITE_24H} pessoas em 24h atingido")


# =====================================================================
# API da Meta
# =====================================================================
def _url():
    return f"https://graph.facebook.com/{WA_API_VERSION}/{WA_PHONE_NUMBER_ID}/messages"


def _post(payload):
    import requests

    try:
        r = requests.post(
            _url(), headers={"Authorization": f"Bearer {WA_TOKEN}"}, json=payload, timeout=30
        )
    except requests.RequestException as e:
        return False, None, None, f"falha de rede: {e}"
    try:
        dados = r.json()
    except ValueError:
        dados = {}
    if r.ok and dados.get("messages"):
        return True, dados["messages"][0].get("id"), None, None
    err = dados.get("error", {})
    codigo = err.get("code", r.status_code)
    detalhe = err.get("message", r.text[:200])
    extra = (err.get("error_data") or {}).get("details")
    if extra:
        detalhe += f" | {extra}"
    return False, None, codigo, detalhe


def api_template(tel, nome):
    componentes = []
    if IMAGEM_LINK:
        componentes.append(
            {"type": "header", "parameters": [{"type": "image", "image": {"link": IMAGEM_LINK}}]}
        )
    if TEMPLATE_USA_NOME:
        componentes.append({"type": "body", "parameters": [{"type": "text", "text": nome}]})
    tpl = {"name": TEMPLATE_NOME, "language": {"code": TEMPLATE_IDIOMA}}
    if componentes:
        tpl["components"] = componentes
    return _post(
        {"messaging_product": "whatsapp", "recipient_type": "individual", "to": tel,
         "type": "template", "template": tpl}
    )


def api_texto(tel, texto):
    return _post({"messaging_product": "whatsapp", "to": tel, "type": "text", "text": {"body": texto}})


def checar_config():
    faltando = [k for k in ("WA_TOKEN", "WA_PHONE_NUMBER_ID", "TEMPLATE_NOME") if not cfg(k)]
    if faltando:
        sair_erro(f"Preencha no .env: {', '.join(faltando)}")
    try:
        import requests  # noqa: F401
    except ImportError:
        sair_erro("Instale a dependencia: pip install requests")


# =====================================================================
# Envio de um contato
# =====================================================================
def atualizar(con, envio_id, **campos):
    campos["atualizado_em"] = agora()
    sets = ", ".join(f"{k}=?" for k in campos)
    con.execute(f"UPDATE envios SET {sets} WHERE id=?", (*campos.values(), envio_id))
    con.commit()


def enviar_um(con, row):
    tel, nome = row["telefone"], primeiro_nome(row["nome"])
    con.execute(
        "UPDATE envios SET status='enviando', tentativas=tentativas+1, atualizado_em=? WHERE id=?",
        (agora(), row["id"]),
    )
    con.commit()
    ok, msg_id, codigo, detalhe = api_template(tel, nome)

    if ok:
        atualizar(con, row["id"], status="enviado", msg_id=msg_id, erro=None, enviado_em=agora())
        log(f"  OK  {tel} ({nome})")
        return "ok"

    def devolver():
        con.execute(
            "UPDATE envios SET status='pendente', tentativas=MAX(tentativas-1,0), erro=?, "
            "atualizado_em=? WHERE id=?",
            (f"{codigo}: {detalhe}", agora(), row["id"]),
        )
        con.commit()

    if codigo is None:
        devolver()
        log(f"  !!  {tel}: {detalhe}")
        return "rede"
    if codigo in ERROS_FATAIS:
        devolver()
        travar(con, f"{codigo} {ERROS_FATAIS[codigo]}")
        log(f"PARADO: erro {codigo} - {ERROS_FATAIS[codigo]}")
        log(f"   Detalhe da Meta: {detalhe}")
        log("   Resolva e rode 'liberar' para destravar.")
        return "parar"
    if codigo in ERROS_ESPERAR:
        devolver()
        log(f"  ..  limite de velocidade ({codigo}), vou esperar")
        return "esperar"
    if codigo in ERROS_OPTOUT:
        marcar_optout(con, tel, f"meta {codigo}: desligou promocoes no WhatsApp")
        con.commit()
        log(f"  --  {tel} desligou promocoes no proprio WhatsApp (sai da lista, nao e bloqueio)")
        return "pulado"
    if codigo in ERROS_LIMITADO:
        atualizar(con, row["id"], status="limitado", erro=f"{codigo}: {detalhe}")
        log(f"  --  {tel} atingiu o limite de marketing por pessoa; nova tentativa em {ESPERA_LIMITADO_H}h")
        return "pulado"
    atualizar(con, row["id"], status="falhou", erro=f"{codigo}: {detalhe}")
    log(f"  XX  {tel}: erro {codigo} - {detalhe}")
    return "falha"


# =====================================================================
# Comandos
# =====================================================================
def cmd_importar(*_):
    arq = BASE / ARQUIVO_CONTATOS
    if not arq.exists():
        sair_erro(f"Arquivo {arq.name} nao encontrado na pasta do script.")
    dm = data_minima()
    linhas = ler_csv(arq)
    if dm and linhas and COLUNA_DATA not in linhas[0]:
        sair_erro(f"O CSV nao tem a coluna '{COLUNA_DATA}' (COLUNA_DATA no .env). "
                  f"Colunas encontradas: {', '.join(linhas[0].keys())}")

    # 1a passada: junta linhas do mesmo telefone, ficando com a data MAIS RECENTE
    contatos, invalidos, repetidos, datas_ruins = {}, 0, 0, 0
    for i, l in enumerate(linhas, start=2):
        bruto = l.get("telefone") or l.get("numero") or l.get("celular") or l.get("whatsapp")
        tel = normalizar_tel(bruto)
        if not tel:
            invalidos += 1
            print(f"  linha {i}: telefone invalido -> {bruto!r}")
            continue
        bruto_data = l.get(COLUNA_DATA, "")
        data = parse_data(bruto_data)
        if bruto_data and not data:
            datas_ruins += 1
            print(f"  linha {i}: data nao reconhecida -> {bruto_data!r}")
        atual = contatos.get(tel)
        if atual is None:
            contatos[tel] = {"nome": l.get("nome", ""), "consentimento": l.get("consentimento", ""),
                             "origem": l.get("origem", ""), "data": data}
            continue
        repetidos += 1
        if data and (not atual["data"] or data > atual["data"]):
            atual["data"] = data
        for k in ("nome", "consentimento", "origem"):
            if not atual[k] and l.get(k):
                atual[k] = l[k]

    # 2a passada: grava no banco e monta a fila
    con = conectar()
    novos = ja_na_fila = fora = antigos = sem_data = sem_consent = 0
    for tel, c in contatos.items():
        if not c["consentimento"]:
            sem_consent += 1
        con.execute(
            """INSERT INTO contatos(telefone, nome, consentimento, origem, data_ref) VALUES(?,?,?,?,?)
               ON CONFLICT(telefone) DO UPDATE SET
                 nome = COALESCE(NULLIF(excluded.nome,''), contatos.nome),
                 consentimento = COALESCE(NULLIF(excluded.consentimento,''), contatos.consentimento),
                 origem = COALESCE(NULLIF(excluded.origem,''), contatos.origem),
                 data_ref = NULLIF(MAX(COALESCE(excluded.data_ref,''), COALESCE(contatos.data_ref,'')), '')""",
            (tel, c["nome"], c["consentimento"], c["origem"], c["data"]),
        )
        if esta_fora(con, tel):
            fora += 1
            continue
        data_ref = con.execute("SELECT data_ref FROM contatos WHERE telefone=?", (tel,)).fetchone()[0]
        if dm:
            if not data_ref and not ACEITAR_SEM_DATA:
                sem_data += 1
                continue
            if data_ref and data_ref < dm:
                antigos += 1
                continue
        cur = con.execute(
            "INSERT OR IGNORE INTO envios(telefone, canal, campanha, atualizado_em) VALUES(?,?,?,?)",
            (tel, CANAL, CAMPANHA, agora()),
        )
        if cur.rowcount:
            novos += 1
        else:
            ja_na_fila += 1
    con.commit()
    con.close()
    print(f"Campanha '{CAMPANHA}' (oficial)")
    if dm:
        print(f"  Periodo .............. '{COLUNA_DATA}' a partir de {br(dm)}")
    print(f"  Novos na fila ........ {novos}")
    print(f"  Ja estavam na fila ... {ja_na_fila}")
    if dm:
        print(f"  Fora do periodo ...... {antigos} (data antes de {br(dm)}, nao entram)")
        if sem_data:
            print(f"  Sem data ............. {sem_data} (nao entram; ACEITAR_SEM_DATA=1 para incluir)")
    print(f"  Pediram para sair .... {fora} (ficam fora das promocoes)")
    print(f"  Linhas repetidas ..... {repetidos} (juntadas, vale a data mais recente)")
    print(f"  Telefones invalidos .. {invalidos}")
    if datas_ruins:
        print(f"  ATENCAO: {datas_ruins} data(s) em formato nao reconhecido. Use 15/03/2024 ou 2024-03-15.")
    if sem_consent:
        print(f"  ATENCAO: {sem_consent} contato(s) sem data de consentimento. Guarde esse registro (LGPD).")


def cmd_simular(*_):
    con = conectar()
    n = len(buscar_pendentes(con, 10**9))
    usados = enviados_24h(con)
    con.close()
    if not n:
        print("Nada pendente nesta campanha. Rode 'importar' primeiro.")
        return
    por_dia = min(n, LIMITE_24H)
    dias = math.ceil(n / LIMITE_24H)
    lotes = math.ceil(por_dia / LOTE)
    seg = por_dia * (INTERVALO_MIN + INTERVALO_MAX) / 2 + (lotes - 1) * (PAUSA_LOTE_MIN + PAUSA_LOTE_MAX) / 2
    print(f"Campanha '{CAMPANHA}' (oficial)")
    print(f"  Pendentes ............ {n}")
    if data_minima():
        print(f"  Periodo .............. '{COLUNA_DATA}' a partir de {br(data_minima())}")
    print(f"  Limite 24h ........... {LIMITE_24H} (ja usados agora: {usados})")
    print(f"  Dias necessarios ..... {dias}")
    print(f"  Tempo por dia ........ ~{seg / 3600:.1f} h ({lotes} lote(s) de ate {LOTE})")
    print(f"  Horario permitido .... {HORA_INICIO}h as {HORA_FIM}h" + ("" if ENVIAR_DOMINGO else ", sem domingo"))
    print(f"  Custo estimado ....... R$ {n * CUSTO_POR_MSG:,.2f} (R$ {CUSTO_POR_MSG} por msg entregue)")
    if seg > (HORA_FIM - HORA_INICIO) * 3600:
        print("  ATENCAO: o tempo por dia passa da janela de horario. Aumente o horario ou reduza as pausas.")


def cmd_teste(numero=None, nome=None, *_):
    if not numero:
        sair_erro("Use: teste 5582999999999 [Nome]")
    checar_config()
    tel = normalizar_tel(numero)
    if not tel:
        sair_erro("Numero invalido.")
    ok, msg_id, codigo, detalhe = api_template(tel, primeiro_nome(nome))
    if ok:
        print(f"Aceito pela Meta (id {msg_id}). Confira se chegou no celular.")
        print("Obs.: 'aceito' ainda nao e 'entregue'. O webhook mostra a entrega.")
    else:
        print(f"Falhou: erro {codigo} - {detalhe}")
        if codigo in ERROS_FATAIS:
            print(f"Dica: {ERROS_FATAIS[codigo]}")


def cmd_enviar(*_):
    checar_config()
    con = conectar()
    n = con.execute(
        "UPDATE envios SET status='verificar', erro='interrompido no meio do envio', atualizado_em=? "
        "WHERE canal=? AND status='enviando'",
        (agora(), CANAL),
    ).rowcount
    con.commit()
    if n:
        log(f"ATENCAO: {n} envio(s) ficaram pela metade -> status 'verificar' (nao serao repetidos sozinhos).")

    enviados = falhas_seguidas = 0
    concluido = False
    try:
        while True:
            motivo = parada_ativa(con)
            if motivo:
                log(f"Disparo TRAVADO: {motivo}")
                log("   Resolva o problema e rode 'liberar' antes de enviar de novo.")
                return
            if not dentro_horario():
                dormir_ate(proximo_inicio(), "Fora do horario permitido")
                continue
            restante = LIMITE_24H - enviados_24h(con)
            if restante <= 0:
                esperar_janela_24h(con)
                continue
            lote = buscar_pendentes(con, min(LOTE, restante))
            if not lote:
                concluido = True
                break
            log(f"Lote de {len(lote)} | usados {LIMITE_24H - restante}/{LIMITE_24H} nas ultimas 24h")
            for i, row in enumerate(lote):
                if parada_ativa(con) or not dentro_horario():
                    break
                r = enviar_um(con, row)
                if r == "parar":
                    return
                if r == "ok":
                    enviados += 1
                    falhas_seguidas = 0
                elif r in ("rede", "esperar"):
                    falhas_seguidas += 1
                    if falhas_seguidas >= 5:
                        log("PARADO: 5 problemas seguidos de rede/velocidade. Tente mais tarde.")
                        return
                    time.sleep(60)
                elif r == "falha":
                    falhas_seguidas += 1
                    if falhas_seguidas >= 10:
                        log("PARADO: 10 falhas seguidas. Provavelmente e configuracao; veja o 'relatorio'.")
                        return
                if i < len(lote) - 1:
                    time.sleep(random.uniform(INTERVALO_MIN, INTERVALO_MAX))
            if buscar_pendentes(con, 1) and dentro_horario() and not parada_ativa(con):
                pausa = random.uniform(PAUSA_LOTE_MIN, PAUSA_LOTE_MAX)
                log(f"Pausa entre lotes: {pausa / 60:.1f} min")
                time.sleep(pausa)
    except KeyboardInterrupt:
        print()
        log("Pausado. Rode 'enviar' de novo para continuar de onde parou.")
    finally:
        log(f"Nesta execucao: {enviados} mensagem(ns) aceita(s) pela Meta.")
        aguardando = con.execute(
            "SELECT COUNT(*) FROM envios WHERE canal=? AND campanha=? AND status='limitado' AND tentativas<2",
            (CANAL, CAMPANHA),
        ).fetchone()[0]
        if aguardando:
            log(f"{aguardando} contato(s) aguardando {ESPERA_LIMITADO_H}h pelo limite por pessoa. "
                "Rode 'enviar' de novo depois.")
        if concluido:
            log("Fila da campanha concluida.")
        con.close()


def cmd_status(*_):
    con = conectar()
    linhas = con.execute(
        "SELECT status, COUNT(*) n FROM envios WHERE canal=? AND campanha=? GROUP BY status ORDER BY n DESC",
        (CANAL, CAMPANHA),
    ).fetchall()
    print(f"Campanha '{CAMPANHA}' (oficial)")
    for r in linhas:
        print(f"  {r['status']:<10} {r['n']}")
    print(f"  Usados nas ultimas 24h: {enviados_24h(con)}/{LIMITE_24H}")
    dm = data_minima()
    print(f"  Prontos para enviar ... {len(buscar_pendentes(con, 10**9))}"
          + (f" ('{COLUNA_DATA}' a partir de {br(dm)})" if dm else ""))
    print(f"  Fora das promocoes (todas as campanhas): "
          f"{con.execute('SELECT COUNT(*) FROM contatos WHERE optout=1').fetchone()[0]}")
    motivo = parada_ativa(con)
    if motivo:
        print(f"  TRAVADO: {motivo}")
    con.close()


def cmd_sair(numero=None, *_):
    tel = normalizar_tel(numero)
    if not tel:
        sair_erro("Use: sair 5582999999999")
    con = conectar()
    marcar_optout(con, tel, "manual")
    con.commit()
    con.close()
    print(f"{tel} fora das promocoes. Nenhum bloqueio no WhatsApp: mensagens manuais seguem normais.")


def cmd_voltar(numero=None, *_):
    tel = normalizar_tel(numero)
    if not tel:
        sair_erro("Use: voltar 5582999999999")
    vs = variantes(tel)
    q = ",".join("?" * len(vs))
    con = conectar()
    con.execute(f"UPDATE contatos SET optout=0, optout_em=NULL, optout_motivo=NULL WHERE telefone IN ({q})", vs)
    con.execute(f"UPDATE envios SET status='pendente' WHERE telefone IN ({q}) AND status='optout'", vs)
    con.commit()
    con.close()
    print(f"{tel} voltou para a lista. Faca isso so se a pessoa pediu para voltar.")


def cmd_reenfileirar(tipo="falhou", *_):
    if tipo not in ("falhou", "verificar"):
        sair_erro("Use: reenfileirar  ou  reenfileirar verificar")
    con = conectar()
    n = con.execute(
        "UPDATE envios SET status='pendente', erro=NULL, atualizado_em=? WHERE canal=? AND campanha=? AND status=?",
        (agora(), CANAL, CAMPANHA, tipo),
    ).rowcount
    con.commit()
    con.close()
    print(f"{n} contato(s) '{tipo}' voltaram para a fila.")
    if tipo == "verificar":
        print("Atencao: alguns desses podem ter recebido. Confira no WhatsApp Manager antes.")


def cmd_liberar(*_):
    con = conectar()
    con.execute("DELETE FROM controle WHERE chave=?", (f"parar_{CANAL}",))
    con.commit()
    con.close()
    print("Destravado. So envie de novo se o problema foi resolvido.")


def cmd_relatorio(*_):
    con = conectar()
    linhas = con.execute(
        """SELECT e.telefone, c.nome, e.status, e.erro, e.tentativas, e.enviado_em, e.atualizado_em,
                  c.consentimento, c.origem, c.optout, c.data_ref
           FROM envios e LEFT JOIN contatos c ON c.telefone = e.telefone
           WHERE e.canal=? AND e.campanha=? ORDER BY e.id""",
        (CANAL, CAMPANHA),
    ).fetchall()
    con.close()
    arq = BASE / f"relatorio_{CAMPANHA}_oficial.csv"
    with open(arq, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["telefone", "nome", "status", "erro", "tentativas", "enviado_em",
                    "atualizado_em", "consentimento", "origem", "fora_promocoes", COLUNA_DATA])
        for r in linhas:
            w.writerow(list(r))
    print(f"Relatorio salvo em {arq.name} ({len(linhas)} linhas).")


# =====================================================================
# Webhook (entregas, falhas e pedidos de SAIR)
# =====================================================================
ORDEM = {"enviando": 0, "verificar": 0, "enviado": 1, "entregue": 2, "lido": 3}
MAPA_STATUS = {"sent": "enviado", "delivered": "entregue", "read": "lido"}


def tratar_status(con, st):
    row = con.execute("SELECT id, status, telefone FROM envios WHERE msg_id=?", (st.get("id"),)).fetchone()
    if not row:
        return
    s = st.get("status")
    if s == "failed":
        err = (st.get("errors") or [{}])[0]
        codigo = err.get("code")
        detalhe = f"{codigo}: {err.get('title', '')} {(err.get('error_data') or {}).get('details', '')}".strip()
        if codigo in ERROS_OPTOUT:
            marcar_optout(con, row["telefone"], f"meta {codigo}")
            novo = "optout"
        elif codigo in ERROS_LIMITADO:
            novo = "limitado"
        else:
            novo = "falhou"
        if codigo in ERROS_FATAIS:
            travar(con, f"{codigo} {ERROS_FATAIS[codigo]}")
            log(f"ALERTA: erro {codigo} recebido. Disparo travado ate rodar 'liberar'.")
        con.execute("UPDATE envios SET status=?, erro=?, atualizado_em=? WHERE id=?",
                    (novo, detalhe, agora(), row["id"]))
        log(f"  {row['telefone']} -> {novo} ({detalhe})")
    elif s in MAPA_STATUS:
        novo = MAPA_STATUS[s]
        if row["status"] in ORDEM and ORDEM[novo] > ORDEM[row["status"]]:
            con.execute("UPDATE envios SET status=?, atualizado_em=? WHERE id=?", (novo, agora(), row["id"]))


def tratar_mensagem(con, m):
    tel = m.get("from", "")
    tipo = m.get("type")
    texto = ""
    if tipo == "text":
        texto = (m.get("text") or {}).get("body", "")
    elif tipo == "button":
        texto = (m.get("button") or {}).get("text", "")
    elif tipo == "interactive":
        it = m.get("interactive") or {}
        texto = (it.get("button_reply") or it.get("list_reply") or {}).get("title", "")
    if tel and norm_texto(texto) in PALAVRAS_SAIR:
        marcar_optout(con, tel, f"respondeu: {texto[:40]}")
        con.commit()
        log(f"  {tel} pediu para sair das promocoes (sem bloqueio no WhatsApp)")
        if CONFIRMAR_SAIDA and WA_TOKEN:
            api_texto(tel, MSG_CONFIRMA_SAIDA)


def processar_webhook(dados):
    con = conectar()
    try:
        for entry in dados.get("entry", []):
            for ch in entry.get("changes", []):
                v = ch.get("value", {})
                for st in v.get("statuses", []):
                    tratar_status(con, st)
                for m in v.get("messages", []):
                    tratar_mensagem(con, m)
        con.commit()
    finally:
        con.close()


def cmd_webhook(*_):
    try:
        from flask import Flask, abort, request
    except ImportError:
        sair_erro("Instale a dependencia: pip install flask")
    if not WEBHOOK_VERIFY_TOKEN:
        sair_erro("Preencha WEBHOOK_VERIFY_TOKEN no .env")
    if not WA_APP_SECRET:
        log("Aviso: sem WA_APP_SECRET a assinatura da Meta nao e conferida. Preencha em producao.")

    app = Flask(__name__)

    @app.get("/webhook")
    def verificar():
        if (request.args.get("hub.mode") == "subscribe"
                and request.args.get("hub.verify_token") == WEBHOOK_VERIFY_TOKEN):
            return request.args.get("hub.challenge", ""), 200
        return "proibido", 403

    @app.post("/webhook")
    def receber():
        corpo = request.get_data()
        if WA_APP_SECRET:
            esperado = "sha256=" + hmac.new(WA_APP_SECRET.encode(), corpo, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(request.headers.get("X-Hub-Signature-256", ""), esperado):
                abort(403)
        try:
            processar_webhook(json.loads(corpo or b"{}"))
        except Exception as e:  # nunca devolver erro para a Meta, senao ela reenvia em loop
            log(f"Erro ao processar webhook: {e}")
        return "ok", 200

    log(f"Webhook ouvindo na porta {WEBHOOK_PORTA} em /webhook (Ctrl+C para sair)")
    app.run(host="0.0.0.0", port=WEBHOOK_PORTA)


COMANDOS = {
    "importar": cmd_importar, "simular": cmd_simular, "teste": cmd_teste, "enviar": cmd_enviar,
    "webhook": cmd_webhook, "status": cmd_status, "sair": cmd_sair, "voltar": cmd_voltar,
    "reenfileirar": cmd_reenfileirar, "liberar": cmd_liberar, "relatorio": cmd_relatorio,
}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMANDOS:
        print(__doc__)
        sys.exit(1)
    COMANDOS[sys.argv[1]](*sys.argv[2:])
