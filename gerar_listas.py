#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GERAR LISTAS - prepara as listas de transmissao do WhatsApp (canal alternativo).

A lista de transmissao so deixa escolher contatos que estao SALVOS na agenda do
celular, no maximo 256 por lista. Salvar 3.000 contatos na mao e inviavel. Entao
este script pega o arquivo ja filtrado e gera arquivos .vcf (agenda) para voce
importar de uma vez. Cada contato recebe um prefixo tipo 'L01' para voce achar
e selecionar a lista inteira rapidinho na hora de criar a transmissao.

Comandos:
  python gerar_listas.py                gera os .vcf a partir do arquivo filtrado
  python gerar_listas.py contatos.csv   usa outro arquivo de entrada

Entrada padrao: saida_<campanha>/contatos_alternativo.csv (feito pelo preparador.py).
Se ele nao existir, tenta o contatos.csv da pasta.

Saida (em 'listas_<campanha>/'):
  L01.vcf, L02.vcf, ...   uma agenda por lista, ate 256 contatos cada
  INSTRUCOES.txt          passo a passo no celular

Como usar no celular:
  1. Mande cada arquivo .vcf para o celular (e-mail, Google Drive, etc.).
  2. Abra o arquivo e escolha importar para os Contatos.
  3. No WhatsApp Business: menu > Nova transmissao.
  4. Na busca, digite o prefixo da lista (ex.: L01) para achar so aquele grupo.
  5. Selecione todos, crie a lista e envie sua mensagem com a imagem.

Sem bibliotecas extras (so o Python).
"""
import csv
import math
import os
import re
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE = Path(__file__).resolve().parent


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


CAMPANHA = cfg("CAMPANHA", "campanha_1")
LISTA_TAM = cfg_int("LISTA_TAM", 256)
# Prefixo que aparece no nome do contato, para achar a lista na busca do WhatsApp
LISTA_PREFIXO = cfg("LISTA_PREFIXO", "L")


def normalizar_tel(bruto):
    d = re.sub(r"\D", "", str(bruto or "")).lstrip("0")
    if len(d) in (10, 11):
        d = "55" + d
    return d if 12 <= len(d) <= 15 else None


def primeiro_e_ultimo(nome):
    partes = (nome or "").split()
    if not partes:
        return "Cliente", ""
    return partes[0].title(), (partes[-1].title() if len(partes) > 1 else "")


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


def achar_entrada(arg):
    if arg:
        p = Path(arg)
        return p if p.is_absolute() else BASE / p
    padrao = BASE / f"saida_{CAMPANHA}" / "contatos_alternativo.csv"
    if padrao.exists():
        return padrao
    return BASE / "contatos.csv"


def escapar_vcf(t):
    return (t or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def vcard(nome_completo, primeiro, ultimo, tel):
    # +tel no formato internacional para o celular reconhecer no WhatsApp
    return (
        "BEGIN:VCARD\r\n"
        "VERSION:3.0\r\n"
        f"N:{escapar_vcf(ultimo)};{escapar_vcf(primeiro)};;;\r\n"
        f"FN:{escapar_vcf(nome_completo)}\r\n"
        f"TEL;TYPE=CELL:+{tel}\r\n"
        "END:VCARD\r\n"
    )


def main():
    entrada = achar_entrada(sys.argv[1] if len(sys.argv) > 1 else None)
    if not entrada.exists():
        print(f"ERRO: nao encontrei o arquivo de entrada '{entrada.name}'.")
        print("Rode antes: python preparador.py gerar   (isso cria o contatos_alternativo.csv)")
        sys.exit(1)

    linhas = ler_csv(entrada)
    vistos, contatos, invalidos = set(), [], 0
    for l in linhas:
        bruto = l.get("telefone") or l.get("numero") or l.get("celular") or l.get("whatsapp")
        tel = normalizar_tel(bruto)
        if not tel or tel in vistos:
            if not tel:
                invalidos += 1
            continue
        vistos.add(tel)
        contatos.append((tel, l.get("nome", "")))

    if not contatos:
        print(f"ERRO: nenhum contato valido em '{entrada.name}'.")
        sys.exit(1)

    total = len(contatos)
    n_listas = math.ceil(total / LISTA_TAM)
    largura = max(2, len(str(n_listas)))
    saida = BASE / f"listas_{CAMPANHA}"
    saida.mkdir(exist_ok=True)

    # limpa .vcf antigos para nao misturar campanhas
    for antigo in saida.glob("*.vcf"):
        antigo.unlink()

    print(f"Campanha '{CAMPANHA}'")
    print(f"  Contatos validos ..... {total}")
    print(f"  Invalidos ignorados .. {invalidos}")
    print(f"  Listas de ate {LISTA_TAM} .... {n_listas}\n")

    resumo_listas = []
    for i in range(n_listas):
        grupo = contatos[i * LISTA_TAM:(i + 1) * LISTA_TAM]
        etiqueta = f"{LISTA_PREFIXO}{i + 1:0{largura}d}"
        partes = []
        for tel, nome in grupo:
            primeiro, ultimo = primeiro_e_ultimo(nome)
            # nome na agenda: "L01 Maria Silva" -> a busca por L01 acha a lista toda
            nome_ag = f"{etiqueta} {primeiro} {ultimo}".strip()
            partes.append(vcard(nome_ag, primeiro, f"{ultimo} {etiqueta}".strip(), tel))
        arq = saida / f"{etiqueta}.vcf"
        arq.write_text("".join(partes), encoding="utf-8")
        print(f"  {arq.name}: {len(grupo)} contatos")
        resumo_listas.append((etiqueta, len(grupo)))

    instrucoes = [
        f"LISTAS DE TRANSMISSAO - campanha '{CAMPANHA}'",
        f"Gerado em {datetime.now():%d/%m/%Y %H:%M}",
        "=" * 58, "",
        f"Total de contatos: {total}",
        f"Listas geradas: {n_listas} (ate {LISTA_TAM} contatos cada)",
        "", "Cada contato foi salvo com um prefixo para voce achar a lista:",
    ]
    instrucoes += [f"  {et} -> {q} contatos" for et, q in resumo_listas]
    instrucoes += [
        "", "PASSO A PASSO NO CELULAR", "-" * 58,
        "1. Envie os arquivos .vcf para o celular da clinica",
        "   (e-mail, Google Drive, WhatsApp Web para si mesmo, cabo USB...).",
        "2. Abra cada .vcf e confirme importar para os Contatos.",
        "3. Abra o WhatsApp Business.",
        "4. Toque nos tres pontinhos > Nova transmissao.",
        f"5. Na busca, digite o prefixo da lista (ex.: {LISTA_PREFIXO}01) para",
        "   aparecer so aquele grupo. Selecione todos e confirme.",
        "6. Escreva a mensagem, anexe a imagem e envie.",
        "7. Repita para cada lista.", "",
        "IMPORTANTE",
        "- Na lista classica (gratuita), so recebe quem tem o SEU numero salvo.",
        "  Quem nao salvou nao recebe e voce nao paga por isso.",
        "- Para alcancar quem nao salvou, o app oferece a transmissao paga",
        "  (cerca de R$ 0,33 por mensagem entregue). Confira o valor na hora.",
        "- Depois da campanha, se quiser, apague os contatos pelo mesmo prefixo.",
    ]
    (saida / "INSTRUCOES.txt").write_text("\n".join(instrucoes) + "\n", encoding="utf-8")

    print(f"\n  INSTRUCOES.txt gerado.")
    print(f"\nTudo salvo em: {saida.name}/")
    print(f"Abra o INSTRUCOES.txt e siga o passo a passo no celular.")


if __name__ == "__main__":
    main()
