#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PREPARADOR - filtra o backup do seu banco e gera os arquivos de cada canal.

Voce exporta a lista do seu sistema (um CSV com telefone, nome e a data do
ultimo atendimento) e este script:
  - fica so com quem tem data de 2024 para ca (ou o corte que voce definir);
  - tira quem ja pediu SAIR (le o disparos.db, se existir);
  - remove telefones repetidos e invalidos;
  - gera um CSV pronto para cada canal e um RESUMO com as quantidades e o custo.

Comandos:
  python preparador.py resumo            so mostra os numeros, nao grava nada
  python preparador.py gerar             gera os arquivos filtrados de todos os canais
  python preparador.py gerar oficial     gera so o arquivo do canal escolhido
                       (oficial | nao_oficial | alternativo)

Arquivos gerados (na pasta 'saida_<campanha>/'):
  contatos_oficial.csv        -> use no disparador_oficial.py
  contatos_nao_oficial.csv    -> use no disparador_nao_oficial.py
  contatos_alternativo.csv    -> use no gerar_listas.py (listas de transmissao)
  descartados.csv             -> quem ficou de fora e por que
  RESUMO.txt                  -> quantidades, dias e custo estimado

Nao precisa de bibliotecas extras (so o Python).
"""
import csv
import math
import os
import re
import sqlite3
import sys
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE = Path(__file__).resolve().parent


# =====================================================================
# Configuracao (.env) - reaproveita o mesmo .env dos outros scripts
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


CAMPANHA = cfg("CAMPANHA", "campanha_1")
BANCO = cfg("BANCO", "disparos.db")
# Arquivo que veio do seu sistema. Troque no .env se o nome for outro.
BACKUP = cfg("BACKUP", "backup_banco.csv")
COLUNA_DATA = cfg("COLUNA_DATA", "ultimo_atendimento").lower()
DATA_MINIMA_CFG = cfg("DATA_MINIMA", "2024-01-01")
ANOS_RETROATIVOS = cfg_int("ANOS_RETROATIVOS", 0)
ACEITAR_SEM_DATA = cfg_bool("ACEITAR_SEM_DATA", 0)

# Numeros usados so para estimar dias e custo no RESUMO
CUSTO_POR_MSG = cfg_float("CUSTO_POR_MSG", 0.3217)
OF_LIMITE_24H = cfg_int("LIMITE_24H", 200)
WEB_LIMITE_24H = cfg_int("WEB_LIMITE_24H", 40)
LISTA_TAM = cfg_int("LISTA_TAM", 256)


# =====================================================================
# Utilidades (mesmas regras dos outros scripts, para bater certinho)
# =====================================================================
def norm_texto(t):
    t = unicodedata.normalize("NFKD", t or "").encode("ascii", "ignore").decode()
    return " ".join(re.sub(r"[^A-Z ]", " ", t.upper()).split())


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
    return v


def parse_data(bruto):
    s = (bruto or "").strip().split(" ")[0].split("T")[0]
    if not s:
        return None
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d.%m.%Y", "%Y/%m/%d", "%d/%m/%y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def br(iso):
    return f"{iso[8:10]}/{iso[5:7]}/{iso[:4]}" if iso else ""


def data_minima():
    if DATA_MINIMA_CFG:
        d = parse_data(DATA_MINIMA_CFG)
        if not d:
            print(f"ERRO: DATA_MINIMA invalida no .env: {DATA_MINIMA_CFG!r} (use 2024-01-01)")
            sys.exit(1)
        return d
    if ANOS_RETROATIVOS > 0:
        hoje = datetime.now().date()
        try:
            return hoje.replace(year=hoje.year - ANOS_RETROATIVOS).isoformat()
        except ValueError:
            return hoje.replace(year=hoje.year - ANOS_RETROATIVOS, day=28).isoformat()
    return None


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


def carregar_optout():
    """Le a lista de quem pediu SAIR no disparos.db (se ele existir)."""
    caminho = BASE / BANCO
    if not caminho.exists():
        return set()
    fora = set()
    try:
        con = sqlite3.connect(caminho)
        for (tel,) in con.execute("SELECT telefone FROM contatos WHERE optout=1"):
            fora |= variantes(tel)
        con.close()
    except sqlite3.Error:
        pass
    return fora


# =====================================================================
# Nucleo: le o backup e separa quem entra de quem sai
# =====================================================================
def processar():
    arq = BASE / BACKUP
    if not arq.exists():
        print(f"ERRO: nao encontrei o backup '{arq.name}' na pasta do script.")
        print("Exporte a lista do seu sistema em CSV com as colunas: telefone; nome; "
              f"{COLUNA_DATA}. Ajuste o nome do arquivo em BACKUP no .env se precisar.")
        sys.exit(1)

    linhas = ler_csv(arq)
    if not linhas:
        print("ERRO: o backup esta vazio.")
        sys.exit(1)

    dm = data_minima()
    if dm and COLUNA_DATA not in linhas[0]:
        print(f"ERRO: o backup nao tem a coluna de data '{COLUNA_DATA}'.")
        print(f"Colunas encontradas: {', '.join(linhas[0].keys())}")
        print("Ajuste COLUNA_DATA no .env para o nome certo, ou deixe DATA_MINIMA vazio "
              "e ANOS_RETROATIVOS=0 para nao filtrar por data.")
        sys.exit(1)

    fora_optout = carregar_optout()
    contatos = {}
    descartados = []
    invalidos = repetidos = 0

    for i, l in enumerate(linhas, start=2):
        bruto = l.get("telefone") or l.get("numero") or l.get("celular") or l.get("whatsapp")
        tel = normalizar_tel(bruto)
        if not tel:
            invalidos += 1
            descartados.append((bruto or "", l.get("nome", ""), "", "telefone invalido"))
            continue
        data = parse_data(l.get(COLUNA_DATA, "")) if dm else None
        atual = contatos.get(tel)
        if atual is None:
            contatos[tel] = {
                "nome": l.get("nome", ""), "data": data,
                "consentimento": l.get("consentimento", ""), "origem": l.get("origem", ""),
            }
        else:
            repetidos += 1
            if data and (not atual["data"] or data > atual["data"]):
                atual["data"] = data
            for k in ("nome", "consentimento", "origem"):
                if not atual[k] and l.get(k):
                    atual[k] = l[k]

    aptos, antigos, sem_data, saiu = [], 0, 0, 0
    for tel, c in contatos.items():
        if tel in fora_optout:
            saiu += 1
            descartados.append((tel, c["nome"], br(c["data"]), "pediu SAIR"))
            continue
        if dm:
            if not c["data"]:
                if not ACEITAR_SEM_DATA:
                    sem_data += 1
                    descartados.append((tel, c["nome"], "", "sem data"))
                    continue
            elif c["data"] < dm:
                antigos += 1
                descartados.append((tel, c["nome"], br(c["data"]), f"antes de {br(dm)}"))
                continue
        aptos.append({"telefone": tel, "nome": c["nome"], "data": c["data"],
                      "consentimento": c["consentimento"], "origem": c["origem"]})

    aptos.sort(key=lambda x: (x["data"] or "", x["nome"]), reverse=True)
    return {
        "aptos": aptos, "descartados": descartados, "dm": dm,
        "total_backup": len(linhas), "unicos": len(contatos),
        "invalidos": invalidos, "repetidos": repetidos,
        "antigos": antigos, "sem_data": sem_data, "saiu": saiu,
    }


# =====================================================================
# Resumo (numeros e custo)
# =====================================================================
def montar_resumo(r):
    n = len(r["aptos"])
    dm = r["dm"]
    L = []
    L.append(f"RESUMO DA CAMPANHA '{CAMPANHA}'")
    L.append(f"Gerado em {datetime.now():%d/%m/%Y %H:%M}")
    L.append("=" * 58)
    L.append("")
    L.append("DE ONDE PARTIMOS")
    L.append(f"  Linhas no backup ......... {r['total_backup']}")
    L.append(f"  Telefones unicos ......... {r['unicos']}")
    L.append(f"  Repetidos (juntados) ..... {r['repetidos']}")
    L.append(f"  Telefones invalidos ...... {r['invalidos']}")
    L.append("")
    L.append("FILTROS APLICADOS")
    if dm:
        L.append(f"  Periodo: '{COLUNA_DATA}' a partir de {br(dm)}")
        L.append(f"    - fora do periodo ...... {r['antigos']}")
        rotulo = "incluidos" if ACEITAR_SEM_DATA else "descartados"
        L.append(f"    - sem data ({rotulo}) ... {r['sem_data']}")
    else:
        L.append("  Periodo: sem filtro de data")
    L.append(f"  Pediram SAIR (opt-out) ... {r['saiu']}")
    L.append("")
    L.append("=" * 58)
    L.append(f"  APTOS A RECEBER .......... {n}")
    L.append("=" * 58)
    L.append("")
    if not n:
        L.append("Ninguem passou pelos filtros. Confira a coluna de data e o corte no .env.")
        return "\n".join(L)

    # Oficial
    dias_of = math.ceil(n / OF_LIMITE_24H)
    L.append("CANAL OFICIAL (WhatsApp Cloud API)")
    L.append(f"  Arquivo .................. contatos_oficial.csv ({n} contatos)")
    L.append(f"  Ritmo .................... {OF_LIMITE_24H}/dia  ->  {dias_of} dia(s)")
    L.append(f"  Custo estimado ........... R$ {n * CUSTO_POR_MSG:,.2f}"
             f"  (R$ {CUSTO_POR_MSG:.4f}/msg entregue)")
    L.append("  Risco de banimento ....... nenhum (canal oficial)")
    L.append("")
    # Nao oficial
    dias_web = math.ceil(n / WEB_LIMITE_24H)
    L.append("CANAL NAO OFICIAL (WhatsApp Web automatizado)")
    L.append(f"  Arquivo .................. contatos_nao_oficial.csv ({n} contatos)")
    L.append(f"  Ritmo .................... {WEB_LIMITE_24H}/dia  ->  {dias_web} dia(s)")
    L.append("  Custo estimado ........... R$ 0 (mas com RISCO ALTO de perder o chip)")
    L.append("  Obs ...................... use um chip separado, nunca o dos exames")
    L.append("")
    # Alternativo
    listas = math.ceil(n / LISTA_TAM)
    L.append("CANAL ALTERNATIVO (listas de transmissao)")
    L.append(f"  Arquivo .................. contatos_alternativo.csv ({n} contatos)")
    L.append(f"  Vai gerar ................ {listas} lista(s) de ate {LISTA_TAM} (rode o gerar_listas.py)")
    L.append("  Custo (lista classica)  .... R$ 0, mas SO recebe quem tem seu numero salvo")
    L.append("  Custo (transmissao paga) . ~R$ 0,33 por entregue, para quem NAO salvou")
    L.append("")
    L.append("Lembrete: 'aptos' e quem passou nos filtros, nao quem vai receber de fato.")
    L.append("Na lista classica, quem nao salvou seu numero nao recebe (e nao paga).")
    return "\n".join(L)


# =====================================================================
# Gravar arquivos
# =====================================================================
CABECALHOS = {
    "oficial": ["telefone", "nome", "consentimento", "origem", COLUNA_DATA],
    "nao_oficial": ["telefone", "nome", "consentimento", "origem", COLUNA_DATA],
    "alternativo": ["telefone", "nome", "consentimento", "origem", COLUNA_DATA],
}
CANAIS = list(CABECALHOS)


def gravar_csv(caminho, cabecalho, linhas):
    with open(caminho, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(cabecalho)
        w.writerows(linhas)


def cmd_resumo(*_):
    r = processar()
    print(montar_resumo(r))


def cmd_gerar(qual=None, *_):
    if qual and qual not in CANAIS:
        print(f"ERRO: canal '{qual}' invalido. Use: {', '.join(CANAIS)} (ou nada para todos).")
        sys.exit(1)
    r = processar()
    if not r["aptos"]:
        print(montar_resumo(r))
        return
    saida = BASE / f"saida_{CAMPANHA}"
    saida.mkdir(exist_ok=True)

    alvos = [qual] if qual else CANAIS
    for canal in alvos:
        linhas = [
            [c["telefone"], c["nome"], c["consentimento"], c["origem"], br(c["data"])]
            for c in r["aptos"]
        ]
        gravar_csv(saida / f"contatos_{canal}.csv", CABECALHOS[canal], linhas)
        print(f"  gerado: contatos_{canal}.csv ({len(linhas)} contatos)")

    gravar_csv(
        saida / "descartados.csv",
        ["telefone", "nome", "data", "motivo"],
        r["descartados"],
    )
    resumo = montar_resumo(r)
    (saida / "RESUMO.txt").write_text(resumo + "\n", encoding="utf-8")
    print(f"  gerado: descartados.csv ({len(r['descartados'])} linhas)")
    print(f"  gerado: RESUMO.txt")
    print(f"\nTudo salvo em: {saida.name}/\n")
    print(resumo)


COMANDOS = {"resumo": cmd_resumo, "gerar": cmd_gerar}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMANDOS:
        print(__doc__)
        sys.exit(1)
    COMANDOS[sys.argv[1]](*sys.argv[2:])
