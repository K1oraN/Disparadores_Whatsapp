#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DISPARADOR NAO OFICIAL - automacao do WhatsApp Web (Playwright)

!! RISCO DE BANIMENTO !!
Automatizar o WhatsApp Web e contra os termos do WhatsApp. O numero pode ser
banido mesmo enviando devagar e so para clientes autorizados. Use um CHIP SO
PARA ISSO. Nunca use o numero pelo qual voces mandam os exames.

Comandos:
  python disparador_nao_oficial.py login              abre o navegador para escanear o QR code
  python disparador_nao_oficial.py importar           le o contatos.csv e monta a fila
  python disparador_nao_oficial.py simular            mostra dias e tempo por dia
  python disparador_nao_oficial.py teste 5582999999999 [Nome]
  python disparador_nao_oficial.py enviar             dispara (Ctrl+C pausa)
  python disparador_nao_oficial.py status
  python disparador_nao_oficial.py sair 5582999999999     tira das promocoes (nao bloqueia)
  python disparador_nao_oficial.py voltar 5582999999999
  python disparador_nao_oficial.py reenfileirar [verificar]
  python disparador_nao_oficial.py relatorio

Usa o MESMO banco do disparador oficial: quem pediu SAIR em um nao recebe no
outro, e ninguem recebe a mesma campanha pelos dois.

Dependencias:
  pip install playwright
  python -m playwright install chromium
"""
import csv
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
from urllib.parse import quote

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE = Path(__file__).resolve().parent
CANAL = "whatsapp_web"

# Seletores do WhatsApp Web. Se o WhatsApp mudar o site, e aqui que se ajusta.
SELETORES = {
    "logado": "#side",
    "caixa": "footer div[contenteditable='true']",
    "popup": "div[data-animate-modal-popup='true']",
    "qr": "canvas",
    "recebida": "div.message-in span.selectable-text",
}


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

# Nao oficial (bem mais conservador que o oficial)
WEB_PERFIL = cfg("WEB_PERFIL", "perfil_whatsapp")
WEB_MENSAGEM = cfg(
    "WEB_MENSAGEM",
    "Ola, {nome}! Temos uma novidade para voce: {link}\\n\\nSe nao quiser receber promocoes, responda SAIR.",
).replace("\\n", "\n")
LIMITE_24H = cfg_int("WEB_LIMITE_24H", 40)
LOTE = cfg_int("WEB_LOTE", 8)
INTERVALO_MIN = cfg_float("WEB_INTERVALO_MIN", 45)
INTERVALO_MAX = cfg_float("WEB_INTERVALO_MAX", 120)
PAUSA_LOTE_MIN = cfg_float("WEB_PAUSA_LOTE_MIN", 900)
PAUSA_LOTE_MAX = cfg_float("WEB_PAUSA_LOTE_MAX", 1800)
ESPERA_LIMITADO_H = 48


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


def montar_mensagem(nome):
    return WEB_MENSAGEM.replace("{nome}", primeiro_nome(nome)).replace("{link}", IMAGEM_LINK)


# =====================================================================
# Banco de dados (o mesmo do oficial)
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


def atualizar(con, envio_id, **campos):
    campos["atualizado_em"] = agora()
    sets = ", ".join(f"{k}=?" for k in campos)
    con.execute(f"UPDATE envios SET {sets} WHERE id=?", (*campos.values(), envio_id))
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
# WhatsApp Web
# =====================================================================
class WhatsAppWeb:
    def __enter__(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            sair_erro("Instale:\n  pip install playwright\n  python -m playwright install chromium")
        self.pw = sync_playwright().start()
        self.ctx = self.pw.chromium.launch_persistent_context(
            str(BASE / WEB_PERFIL), headless=False, viewport=None, locale="pt-BR"
        )
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        self.page.goto("https://web.whatsapp.com", wait_until="domcontentloaded")
        log("Abrindo o WhatsApp Web. Se aparecer o QR code, escaneie com o celular do chip de DISPARO.")
        try:
            self.page.wait_for_selector(SELETORES["logado"], timeout=180_000)
        except Exception:
            self.__exit__()
            sair_erro("Nao entrou no WhatsApp Web em 3 minutos.")
        log("Conectado.")
        return self

    def __exit__(self, *_):
        for fechar in (lambda: self.ctx.close(), lambda: self.pw.stop()):
            try:
                fechar()
            except Exception:
                pass

    def pediu_sair(self):
        try:
            textos = self.page.locator(SELETORES["recebida"]).all_inner_texts()[-3:]
        except Exception:
            return False
        return any(norm_texto(t) in PALAVRAS_SAIR for t in textos)

    def enviar(self, tel, texto):
        p = self.page
        p.goto(f"https://web.whatsapp.com/send?phone={tel}&text={quote(texto)}", wait_until="domcontentloaded")
        limite = time.time() + 60
        sem_login = 0
        while True:
            if p.locator(SELETORES["caixa"]).count():
                break
            pop = p.locator(SELETORES["popup"])
            if pop.count():
                if "INVALID" in norm_texto(pop.first.inner_text()):
                    return "invalido"
            if p.locator(SELETORES["qr"]).count() and not p.locator(SELETORES["logado"]).count():
                sem_login += 1
                if sem_login >= 5:
                    return "deslogado"
            if time.time() > limite:
                return "timeout"
            time.sleep(1)

        time.sleep(random.uniform(3, 6))  # carrega historico e a previa do link
        if self.pediu_sair():
            return "optout"
        caixa = p.locator(SELETORES["caixa"]).last
        if not caixa.inner_text().strip():
            return "vazio"
        caixa.click()
        p.keyboard.press("Enter")
        for _ in range(20):
            time.sleep(1)
            try:
                if not caixa.inner_text().strip():
                    return "ok"
            except Exception:
                pass
        return "incerto"


# =====================================================================
# Comandos
# =====================================================================
def cmd_login(*_):
    with WhatsAppWeb():
        log("Sessao salva. Nas proximas vezes nao precisa escanear de novo.")
        time.sleep(5)


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
    print(f"Campanha '{CAMPANHA}' (nao oficial)")
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
    seg = por_dia * ((INTERVALO_MIN + INTERVALO_MAX) / 2 + 20) + (lotes - 1) * (PAUSA_LOTE_MIN + PAUSA_LOTE_MAX) / 2
    print(f"Campanha '{CAMPANHA}' (nao oficial)")
    print(f"  Pendentes ............ {n}")
    if data_minima():
        print(f"  Periodo .............. '{COLUNA_DATA}' a partir de {br(data_minima())}")
    print(f"  Limite 24h ........... {LIMITE_24H} (ja usados agora: {usados})")
    print(f"  Dias necessarios ..... {dias}")
    print(f"  Tempo por dia ........ ~{seg / 3600:.1f} h ({lotes} lote(s) de ate {LOTE})")
    print("  Custo ................ sem cobranca por mensagem, mas com risco de banimento do chip")
    print("\nPrevia da mensagem:\n" + montar_mensagem("Maria"))


def cmd_teste(numero=None, nome=None, *_):
    tel = normalizar_tel(numero)
    if not tel:
        sair_erro("Use: teste 5582999999999 [Nome]")
    with WhatsAppWeb() as wa:
        r = wa.enviar(tel, montar_mensagem(nome))
        print(f"Resultado: {r}")
        time.sleep(5)


def cmd_enviar(*_):
    if not IMAGEM_LINK and "{link}" in WEB_MENSAGEM:
        sair_erro("Preencha IMAGEM_LINK no .env (a mensagem usa {link}).")
    con = conectar()
    n = con.execute(
        "UPDATE envios SET status='verificar', erro='interrompido no meio do envio', atualizado_em=? "
        "WHERE canal=? AND status='enviando'",
        (agora(), CANAL),
    ).rowcount
    con.commit()
    if n:
        log(f"ATENCAO: {n} envio(s) ficaram pela metade -> status 'verificar' (nao serao repetidos sozinhos).")

    enviados = problemas_seguidos = 0
    concluido = False
    try:
        with WhatsAppWeb() as wa:
            while True:
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
                    if not dentro_horario():
                        break
                    tel = row["telefone"]
                    con.execute(
                        "UPDATE envios SET status='enviando', tentativas=tentativas+1, atualizado_em=? WHERE id=?",
                        (agora(), row["id"]),
                    )
                    con.commit()
                    try:
                        r = wa.enviar(tel, montar_mensagem(row["nome"]))
                    except Exception as e:
                        r = "timeout"
                        log(f"  !!  erro no navegador: {e}")

                    if r == "ok":
                        atualizar(con, row["id"], status="enviado", erro=None, enviado_em=agora())
                        log(f"  OK  {tel} ({primeiro_nome(row['nome'])})")
                        enviados += 1
                        problemas_seguidos = 0
                    elif r == "invalido":
                        atualizar(con, row["id"], status="falhou", erro="numero nao tem WhatsApp")
                        log(f"  XX  {tel}: nao tem WhatsApp")
                    elif r == "optout":
                        marcar_optout(con, tel, "respondeu SAIR (WhatsApp Web)")
                        con.commit()
                        log(f"  --  {tel} tinha pedido SAIR: fora das promocoes (sem bloqueio)")
                    elif r == "incerto":
                        atualizar(con, row["id"], status="verificar", erro="nao confirmou o envio")
                        log(f"  ??  {tel}: nao deu para confirmar; marcado 'verificar'")
                        problemas_seguidos += 1
                    elif r == "deslogado":
                        atualizar(con, row["id"], status="pendente", erro=None)
                        log("PARADO: o WhatsApp Web desconectou. Pode ser queda de sessao ou banimento.")
                        log("   Abra o WhatsApp no celular do chip antes de tentar de novo.")
                        return
                    else:  # timeout / vazio
                        atualizar(con, row["id"], status="pendente", erro=f"tentativa: {r}")
                        log(f"  !!  {tel}: {r}, volta para a fila")
                        problemas_seguidos += 1

                    if problemas_seguidos >= 3:
                        log("PARADO: 3 problemas seguidos. Confira o navegador (site mudou? sessao caiu?).")
                        return
                    if i < len(lote) - 1:
                        time.sleep(random.uniform(INTERVALO_MIN, INTERVALO_MAX))

                if buscar_pendentes(con, 1) and dentro_horario():
                    pausa = random.uniform(PAUSA_LOTE_MIN, PAUSA_LOTE_MAX)
                    log(f"Pausa entre lotes: {pausa / 60:.1f} min")
                    time.sleep(pausa)
    except KeyboardInterrupt:
        print()
        log("Pausado. Rode 'enviar' de novo para continuar de onde parou.")
    finally:
        log(f"Nesta execucao: {enviados} mensagem(ns) enviada(s).")
        if concluido:
            log("Fila da campanha concluida.")
        con.close()


def cmd_status(*_):
    con = conectar()
    linhas = con.execute(
        "SELECT status, COUNT(*) n FROM envios WHERE canal=? AND campanha=? GROUP BY status ORDER BY n DESC",
        (CANAL, CAMPANHA),
    ).fetchall()
    print(f"Campanha '{CAMPANHA}' (nao oficial)")
    for r in linhas:
        print(f"  {r['status']:<10} {r['n']}")
    print(f"  Usados nas ultimas 24h: {enviados_24h(con)}/{LIMITE_24H}")
    dm = data_minima()
    print(f"  Prontos para enviar ... {len(buscar_pendentes(con, 10**9))}"
          + (f" ('{COLUNA_DATA}' a partir de {br(dm)})" if dm else ""))
    print(f"  Fora das promocoes (todas as campanhas): "
          f"{con.execute('SELECT COUNT(*) FROM contatos WHERE optout=1').fetchone()[0]}")
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
        print("Atencao: alguns desses podem ter recebido. Confira as conversas no celular antes.")


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
    arq = BASE / f"relatorio_{CAMPANHA}_nao_oficial.csv"
    with open(arq, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["telefone", "nome", "status", "erro", "tentativas", "enviado_em",
                    "atualizado_em", "consentimento", "origem", "fora_promocoes", COLUNA_DATA])
        for r in linhas:
            w.writerow(list(r))
    print(f"Relatorio salvo em {arq.name} ({len(linhas)} linhas).")


COMANDOS = {
    "login": cmd_login, "importar": cmd_importar, "simular": cmd_simular, "teste": cmd_teste,
    "enviar": cmd_enviar, "status": cmd_status, "sair": cmd_sair, "voltar": cmd_voltar,
    "reenfileirar": cmd_reenfileirar, "relatorio": cmd_relatorio,
}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMANDOS:
        print(__doc__)
        sys.exit(1)
    COMANDOS[sys.argv[1]](*sys.argv[2:])
