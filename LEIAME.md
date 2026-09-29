# Disparador de mensagens — guia de uso

Sistema para enviar uma mensagem (com imagem e nome do cliente) para uma lista
grande, por três caminhos diferentes, sem passar dos limites que geram bloqueio.

Todos os scripts usam **um único arquivo de configuração** (`.env`) e o **mesmo
banco** (`disparos.db`). Assim, quem pede SAIR em um canal não recebe em nenhum
outro, e ninguém recebe a mesma campanha duas vezes.

---

## Os arquivos

| Arquivo | Para que serve |
|---|---|
| `preparador.py` | Lê o backup do seu sistema, aplica os filtros (período, opt-out, repetidos) e gera um arquivo pronto para cada canal, mais um **RESUMO** com quantidades e custo. **Comece por aqui.** |
| `disparador_oficial.py` | Envia pela API oficial da Meta (WhatsApp Cloud API). Sem risco de banimento. Pago por mensagem. |
| `disparador_nao_oficial.py` | Automatiza o WhatsApp Web. Grátis, mas com **risco alto de banir o número**. |
| `gerar_listas.py` | Prepara o canal alternativo: divide os contatos em grupos de 256 e cria arquivos de agenda (`.vcf`) para você montar as listas de transmissão no celular. |
| `.env` | Onde você configura tudo (a partir do `exemplo.env`). |
| `backup_banco.csv` | O que você exporta do seu sistema. |

---

## Qual canal usar

| | Oficial (API) | Não oficial (Web) | Alternativo (transmissão) |
|---|---|---|---|
| **Custo** | ~R$ 0,32 por msg | R$ 0 (só o chip) | R$ 0 para quem salvou; ~R$ 0,33 para quem não salvou |
| **Risco de banir** | Nenhum | **Alto** | Nenhum |
| **Imagem na mensagem** | Sim | Só o link | Sim |
| **Chega em quem não salvou** | Sim | Sim | Só na versão paga |
| **Trabalho** | Configura uma vez, roda sozinho | Roda sozinho | Manual no celular |
| **Limite de início** | 250/dia (conta nova) | você define (arriscado subir) | 256 por lista, várias listas |

Recomendação geral: **oficial** se puder pagar e quiser tudo automático;
**alternativo** para gastar o mínimo aproveitando quem já tem seu número salvo;
**não oficial** só em último caso, com um chip separado.

---

## Fluxo de uso (o caminho normal)

### Passo 1 — Configurar (uma vez)

Renomeie `exemplo.env` para `.env` e preencha. As chaves mais importantes:

```
CAMPANHA=promo_outubro_2026      # troque a cada campanha nova
BACKUP=backup_banco.csv          # o arquivo que sai do seu sistema
COLUNA_DATA=ultimo_atendimento   # nome da coluna de data no seu backup
DATA_MINIMA=2024-01-01           # só entra quem tem data a partir daqui
IMAGEM_LINK=https://.../promo.jpg
```

### Passo 2 — Exportar o backup

Exporte do seu sistema um CSV com, no mínimo, **telefone**, **nome** e a
**coluna de data**. Pode ter mais colunas, elas são ignoradas. Formatos de
telefone e data variados são aceitos (o script normaliza). Salve com o nome que
está em `BACKUP`.

### Passo 3 — Ver os números antes de decidir

```
python preparador.py resumo
```

Isso não grava nada. Só mostra quantos passaram nos filtros, quantos dias cada
canal levaria e o custo estimado. Use para decidir o canal.

### Passo 4 — Gerar os arquivos filtrados

```
python preparador.py gerar
```

Cria a pasta `saida_<campanha>/` com:
- `contatos_oficial.csv`, `contatos_nao_oficial.csv`, `contatos_alternativo.csv`
- `descartados.csv` (quem ficou de fora e por quê)
- `RESUMO.txt`

Se quiser só um canal: `python preparador.py gerar oficial`

### Passo 5 — Disparar pelo canal escolhido

Aponte o disparador para o arquivo filtrado. No `.env`, ajuste:
```
ARQUIVO_CONTATOS=saida_promo_outubro_2026/contatos_oficial.csv
```
Depois siga a seção do canal, abaixo.

---

## Canal oficial (`disparador_oficial.py`)

**Instalar:** `pip install requests flask`

**Configurar no `.env`:** `WA_TOKEN`, `WA_PHONE_NUMBER_ID`, `TEMPLATE_NOME`.
São dados que você pega ao criar o app em developers.facebook.com e o template
no WhatsApp Manager.

**Ordem dos comandos:**
```
python disparador_oficial.py importar          # monta a fila a partir do CSV
python disparador_oficial.py simular           # confirma dias e custo
python disparador_oficial.py teste 5582999999999 SeuNome   # manda 1 pra você
python disparador_oficial.py enviar            # dispara de verdade
```

Se cair ou você apertar Ctrl+C, rode `enviar` de novo: continua de onde parou.

**Webhook (opcional, mas recomendado):** mostra quem recebeu/leu e captura os
pedidos de SAIR automaticamente. Precisa de um endereço público com HTTPS
(ex.: `ngrok http 8000`). Rode `python disparador_oficial.py webhook` e cole a
URL com `/webhook` no painel do app, campo "messages".

**Se travar:** ao receber um erro grave (spam, template pausado), o script para
sozinho. Resolva no WhatsApp Manager e rode `python disparador_oficial.py liberar`.

---

## Canal não oficial (`disparador_nao_oficial.py`)

> ⚠️ Vai contra os termos do WhatsApp. O número pode ser banido mesmo devagar.
> **Use um chip só para isso. Nunca o número dos exames.**

**Instalar:**
```
pip install playwright
python -m playwright install chromium
```

**Comandos:**
```
python disparador_nao_oficial.py login         # escaneia o QR uma vez
python disparador_nao_oficial.py importar
python disparador_nao_oficial.py simular
python disparador_nao_oficial.py teste 5582999999999 SeuNome
python disparador_nao_oficial.py enviar
```

Já vem bem mais lento que o oficial (padrão: 40/dia). A mensagem é texto com o
link da imagem; ela aparece como prévia, não embutida.

---

## Canal alternativo — listas de transmissão (`gerar_listas.py`)

O mais barato quando boa parte dos clientes já tem o número da clínica salvo.

```
python preparador.py gerar alternativo    # cria o contatos_alternativo.csv
python gerar_listas.py                     # divide em grupos de 256 (.vcf)
```

Gera a pasta `listas_<campanha>/` com `L01.vcf`, `L02.vcf`, ... e um
`INSTRUCOES.txt`. No celular:

1. Envie os `.vcf` para o aparelho e importe para os Contatos.
2. WhatsApp Business → menu → **Nova transmissão**.
3. Na busca, digite o prefixo (ex.: `L01`) para achar só aquele grupo.
4. Selecione todos, crie a lista, escreva a mensagem com a imagem e envie.

**Regras que valem lembrar:**
- Na lista **clássica** (grátis), só recebe quem tem **seu número salvo**. Quem
  não salvou não recebe e você não paga.
- Para alcançar quem não salvou, o app oferece a **transmissão paga**
  (cerca de R$ 0,33 por mensagem entregue). Confirme o valor na tela antes.

---

## Personalização (tudo no `.env`)

**Filtro de período**
- `COLUNA_DATA` — nome exato da coluna de data no seu backup.
- `DATA_MINIMA=2024-01-01` — corte fixo.
- `ANOS_RETROATIVOS=2` — corte móvel (últimos 2 anos a partir de hoje). Para
  usar, deixe `DATA_MINIMA` vazio.
- `ACEITAR_SEM_DATA=1` — inclui quem está sem data no backup.

**Quem sai da lista**
- `PALAVRAS_SAIR=SAIR,PARAR,STOP,CANCELAR` — respostas que tiram a pessoa das
  promoções. Não bloqueia ninguém no WhatsApp; só evita mandar promoção de novo.

**Ritmo (oficial)**
- `LIMITE_24H` — pessoas por dia (comece em 200; suba conforme o WhatsApp
  Manager liberar).
- `LOTE`, `INTERVALO_MIN/MAX`, `PAUSA_LOTE_MIN/MAX` — tamanho do lote e pausas.

**Ritmo (não oficial)** — as mesmas ideias, com prefixo `WEB_`
(`WEB_LIMITE_24H`, `WEB_LOTE`, etc.). Mantenha baixo.

**Horário de envio**
- `HORA_INICIO`, `HORA_FIM`, `ENVIAR_DOMINGO` — janela em que pode disparar.

**Mensagem (não oficial)**
- `WEB_MENSAGEM` — use `{nome}` e `{link}`. `\n` vira quebra de linha.

**Listas**
- `LISTA_TAM=256` — tamanho de cada lista.
- `LISTA_PREFIXO=L` — prefixo que aparece no nome do contato.

**Custo**
- `CUSTO_POR_MSG` — preço por mensagem, só para a estimativa do resumo.

---

## Plano sugerido: 3.000 pessoas em 5 dias

Para o prazo curto, misturar canais sai mais barato que um só:

1. **Dia 1 — grátis.** Gere as listas (`gerar_listas.py`) e mande a **lista de
   transmissão clássica** do número dos exames. Quem já salvou o número recebe
   sem custo. Isso costuma cobrir boa parte da base.
2. **Dias 2 a 5 — pago só para o resto.** Para quem não recebeu (não tinha o
   número salvo), use a **transmissão paga** do app ou a **API oficial**. Custo
   aproximado de R$ 0,33 por mensagem entregue, só sobre esse grupo menor.

Assim você paga apenas por quem a parte grátis não alcançou, em vez de pagar
pelos 3.000. O `preparador.py resumo` mostra os números reais da sua lista para
você calcular direitinho.

> Não recomendo despejar 3.000 pelo canal não oficial em 5 dias, nem com chip
> antigo: o volume alto para quem não salvou o número é o que mais gera denúncia
> e banimento. Se o chip cair no meio, você perde os dias que faltam.

---

## Perguntas rápidas

**Uma pessoa recebeu duas vezes?** Não deveria. O banco guarda cada envio por
campanha; ao rerodar, ele pula quem já recebeu. Isso vale inclusive entre canais.

**Como faço uma campanha nova?** Troque `CAMPANHA` no `.env` e recomece do
`preparador.py`. O histórico da campanha anterior fica guardado.

**Alguém pediu para voltar a receber?** `python disparador_oficial.py voltar
5582999999999` (ou pelo não oficial). Faça só se a pessoa pediu.

**LGPD.** Guarde o registro de quando e como cada cliente autorizou o contato.
O backup pode trazer a coluna `consentimento`, que é levada para os relatórios.
Atenda os pedidos de SAIR na hora — o sistema já faz isso.
