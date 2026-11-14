"""Robô do app Filtro de Ações (Diego, 14/11/2026). Gera site/data.json.

Regras definidas pelo Diego:
  Lucro consecutivo: lucro líquido > 0 nos 3 últimos anos fiscais E nos últimos 12 meses.
  Mid cap: valor de mercado de US$ 2 a 10 bi (o app deixa mudar a faixa).
  Tendência de alta: os 2 últimos fundos ascendentes e os 2 últimos topos ascendentes, e o preço acima do último fundo.
  Recuo: preço abaixo do último topo e acima do último fundo; % = quanto devolveu da última perna de alta.
  Diário e semanal.
Topo/fundo = candle cuja máxima (mínima) é maior (menor) que as dos 2 candles de cada lado (fractal de 5 candles).

Fontes (todas aceitam robô identificado):
  SEC: lista de ações da NYSE/Nasdaq, setor (código SIC), país, número de ações e lucro líquido (10-K/10-Q/20-F).
  Yahoo (yfinance): preços diários de 2 anos; o semanal é montado a partir do diário (semana fecha na sexta).
  Valor de mercado = número de ações (SEC) x último preço.

uso:  python robo/atualizar.py            (preços todo dia; dados da SEC só se o cache tiver mais de 7 dias)
      python robo/atualizar.py --sec      (força buscar os dados da SEC de novo)
      python robo/atualizar.py --teste PBF HG GCT   (só alguns tickers, mostra na tela e não grava o site)
"""
import argparse
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import yfinance as yf

sys.stdout.reconfigure(encoding="utf-8")
RAIZ = Path(__file__).resolve().parent.parent
CACHE = RAIZ / "robo" / "cache"
SITE = RAIZ / "site"
UA = {"User-Agent": "filtro-acoes app (contato: filtro.acoes.app@gmail.com)", "Accept": "application/json"}
CAP_MIN = 0.3e9                      # universo: valor de mercado a partir de US$ 300 mi
BOLSAS = {"Nasdaq", "NYSE", "CBOE"}
LUCRO = [("us-gaap", "NetIncomeLoss"), ("us-gaap", "NetIncomeLossAvailableToCommonStockholdersBasic"),
         ("us-gaap", "ProfitLoss"),                # inclui sócios minoritários: o robô desconta a parte deles
         ("ifrs-full", "ProfitLossAttributableToOwnersOfParent"), ("ifrs-full", "ProfitLoss")]
ACOES = [("dei", "EntityCommonStockSharesOutstanding"),
         ("us-gaap", "WeightedAverageNumberOfSharesOutstandingBasic"),
         ("ifrs-full", "WeightedAverageShares")]


_trava, _ultimo = threading.Lock(), [0.0]


def _vez():
    """Espaça os pedidos (todas as linhas juntas) para ficar abaixo de 10 por segundo, o limite da SEC."""
    with _trava:
        espera = _ultimo[0] + 0.12 - time.time()
        if espera > 0:
            time.sleep(espera)
        _ultimo[0] = time.time()


def baixar_json(url, tentativas=4):
    for k in range(tentativas):
        try:
            _vez()
            return json.load(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            time.sleep(2 * (k + 1))
        except Exception:
            time.sleep(2 * (k + 1))
    return None


# ------------------------------------------------------------------ setor a partir do código SIC
def setor(sic):
    s = int(sic) if str(sic).isdigit() else -1
    faixas = [((100, 999), "Agro"), ((1000, 1299), "Mineração"), ((1300, 1399), "Energia"),
              ((1400, 1499), "Mineração"), ((1500, 1799), "Construção"), ((2830, 2836), "Saúde"),
              ((2911, 2911), "Energia"), ((3570, 3579), "Tecnologia"), ((3600, 3699), "Tecnologia"),
              ((3812, 3812), "Indústria"), ((3820, 3829), "Tecnologia"), ((3840, 3851), "Saúde"),
              ((2000, 3999), "Indústria"), ((4000, 4799), "Transporte"), ((4800, 4899), "Telecom"),
              ((4900, 4999), "Utilidades"), ((5000, 5199), "Comércio"), ((5200, 5999), "Varejo"),
              ((6000, 6411), "Financeiro"), ((6500, 6553), "Imobiliário"), ((6798, 6798), "Imobiliário"),
              ((6412, 6799), "Financeiro"), ((7370, 7379), "Tecnologia"), ((8000, 8099), "Saúde"),
              ((7000, 8999), "Serviços")]
    for (a, b), nome in faixas:
        if a <= s <= b:
            return nome
    return "Outros"


# ------------------------------------------------------------------ dados da SEC por empresa
def dias(a, b):
    return (pd.Timestamp(b) - pd.Timestamp(a)).days


def conceito(cik, tax, conc):
    d = baixar_json(f"https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:010d}/{tax}/{conc}.json")
    if not d:
        return None
    return [x for u in d["units"].values() for x in u]


def lucros(cik):
    """(anos [(fim, valor)] do mais novo ao mais velho, ttm, conceito, base) ou None."""
    for tax, conc in LUCRO:
        fatos = [x for x in (conceito(cik, tax, conc) or [])
                 if x.get("start") and x.get("form", "").startswith(("10-K", "10-Q", "20-F", "40-F"))]
        if not fatos:
            continue
        df = pd.DataFrame(fatos)
        df["dur"] = [dias(a, b) for a, b in zip(df["start"], df["end"])]
        df = df.sort_values("filed").drop_duplicates(["start", "end"], keep="last")   # vale a versão mais nova
        if conc == "ProfitLoss":                   # tira a parte dos sócios minoritários, quando informada
            nci = {}
            for c in ("NetIncomeLossAttributableToNoncontrollingInterest",
                      "NetIncomeLossAttributableToRedeemableNoncontrollingInterest"):
                for x in conceito(cik, "us-gaap", c) or []:
                    if x.get("start"):
                        k = (x["start"], x["end"])      # as duas tags podem repetir o mesmo valor: usa a maior
                        nci[k] = max(nci.get(k, float("-inf")), float(x["val"]))
            df["val"] = [float(v) - nci.get((a, b), 0.0) for v, a, b in zip(df["val"], df["start"], df["end"])]
        anual = df[(df["dur"] >= 350) & (df["dur"] <= 380)].sort_values("end", ascending=False).drop_duplicates("end")
        anos = [(r.end, float(r.val)) for r in anual.itertuples()][:4]
        if not anos:
            continue
        # últimos 12 meses = último ano fechado + acumulado do ano corrente - mesmo acumulado do ano anterior
        ult = df.sort_values(["end", "dur"], ascending=[False, False]).iloc[0]
        ttm, base = None, None
        if 350 <= ult.dur <= 380:
            ttm, base = float(ult.val), f"ano até {ult.end}"
        elif ult.dur > 60:
            fy = anual[anual["end"] < ult.end]
            alvo = pd.Timestamp(ult.end) - pd.DateOffset(years=1)
            ant = df[(abs(df["dur"] - ult.dur) <= 15) & (abs(pd.to_datetime(df["end"]) - alvo).dt.days <= 20)]
            if len(fy) and len(ant):
                ttm = float(fy.iloc[0].val) + float(ult.val) - float(ant.iloc[0].val)
                base = f"12 meses até {ult.end}"
        if ttm is None:
            ttm, base = anos[0][1], f"só o ano até {anos[0][0]}"
        return {"anos": anos, "ttm": ttm, "conc": conc, "base": base}
    return None


def acoes_em_circulacao(cik):
    """Maior entre o nº de ações da capa do relatório (às vezes só de uma classe) e a média de ações do
    lucro por ação (soma todas as classes). Considera só dados dos últimos ~15 meses."""
    achados = []
    for tax, conc in ACOES:
        fatos = conceito(cik, tax, conc)
        if not fatos:
            continue
        df = pd.DataFrame(fatos).sort_values(["end", "filed"])
        df = df[df["form"].str.startswith(("10-Q", "10-K", "20-F", "40-F"))]
        df = df[pd.to_datetime(df["end"]) >= pd.Timestamp.today() - pd.DateOffset(months=15)]
        if len(df):
            r = df.iloc[-1]
            achados.append((float(r["val"]), r["end"]))
        if len(achados) == 2:
            break
    return max(achados) if achados else (None, None)


def perfil(cik):
    s = baixar_json(f"https://data.sec.gov/submissions/CIK{cik:010d}.json") or {}
    end = (s.get("addresses") or {}).get("business") or {}
    pais = end.get("stateOrCountryDescription") or ""
    if end.get("isForeignLocation") in (0, None) and len(end.get("stateOrCountry") or "") == 2:
        pais = "EUA"
    return {"nome": s.get("name") or "", "sic": s.get("sic") or "", "ind": s.get("sicDescription") or "",
            "pais": pais}


def trimestres(n=5):
    """Os n últimos trimestres do calendário (para as consultas em massa da SEC), do mais novo ao mais velho."""
    t = pd.Timestamp.today().to_period("Q")
    return [f"CY{(t - k).year}Q{(t - k).quarter}" for k in range(1, n + 1)]


def acoes_em_massa():
    """Nº de ações de todas as empresas em poucos pedidos: o maior entre o da capa do relatório (dei) e a
    média ponderada do lucro por ação (soma todas as classes). Usa o dado mais recente de cada empresa."""
    capa, media = {}, {}
    for q in trimestres():
        for alvo, url in ((capa, f"https://data.sec.gov/api/xbrl/frames/dei/EntityCommonStockSharesOutstanding/shares/{q}I.json"),
                          (media, f"https://data.sec.gov/api/xbrl/frames/us-gaap/WeightedAverageNumberOfSharesOutstandingBasic/shares/{q}.json")):
            d = baixar_json(url) or {"data": []}
            for x in d["data"]:
                alvo.setdefault(int(x["cik"]), float(x["val"]))     # o 1º visto é o mais recente
    out = {}
    for c in set(capa) | set(media):
        v = max([x for x in (capa.get(c), media.get(c)) if x and x > 0], default=None)
        if v:
            out[c] = v
    return out


def atualizar_sec(lista, forcar=False):
    """Setor/país (guardado para sempre) e lucros (refeitos a cada 7 dias) das empresas da lista."""
    arq = CACHE / "sec.json"
    cache = json.loads(arq.read_text(encoding="utf-8")) if arq.exists() else {"data": "2000-01-01", "emp": {}}
    velho = forcar or (date.today() - date.fromisoformat(cache["data"])).days >= 7
    emp = cache["emp"]
    fila = [r for r in lista if velho or str(r["cik"]) not in emp or "sic" not in emp[str(r["cik"])]]
    print(f"SEC: {len(fila)} empresas para buscar ({'atualização semanal' if velho else 'só as novas'})", flush=True)
    feitos, t0 = [0], time.time()

    def uma(r):
        k = str(r["cik"])
        antigo = emp.get(k) or {}
        pf = antigo if "sic" in antigo else perfil(r["cik"])
        novo = {"nome": pf.get("nome"), "sic": pf.get("sic"), "ind": pf.get("ind"), "pais": pf.get("pais"),
                "lucro": lucros(r["cik"])}
        if not antigo.get("acoes") and not r.get("acoes"):           # fora da consulta em massa: busca sozinha
            novo["acoes"] = acoes_em_circulacao(r["cik"])[0]
        emp[k] = novo
        feitos[0] += 1
        if feitos[0] % 250 == 0:
            print(f"  SEC {feitos[0]}/{len(fila)}  {time.time() - t0:.0f}s", flush=True)

    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(uma, fila))
    if velho:
        cache["data"] = date.today().isoformat()
    CACHE.mkdir(parents=True, exist_ok=True)
    arq.write_text(json.dumps(cache), encoding="utf-8")
    return emp


# ------------------------------------------------------------------ universo (SEC)
def universo():
    e = baixar_json("https://www.sec.gov/files/company_tickers_exchange.json")
    vistos, out = set(), []
    for cik, nome, tk, bolsa in e["data"]:
        if bolsa not in BOLSAS or cik in vistos or not tk:
            continue
        vistos.add(cik)                            # 1º ticker de cada empresa = ação ordinária principal
        out.append({"cik": int(cik), "nome": nome, "ticker": tk.replace("-", "."), "yahoo": tk.replace(".", "-"),
                    "bolsa": bolsa})
    return out


# ------------------------------------------------------------------ preços e topos/fundos
def pivos(h, l, n=2):
    """Índices de topos e fundos confirmados (fractal: n candles de cada lado)."""
    topos, fundos = [], []
    for i in range(n, len(h) - n):
        if h[i] > max(h[i - n:i]) and h[i] >= max(h[i + 1:i + n + 1]):
            topos.append(i)
        if l[i] < min(l[i - n:i]) and l[i] <= min(l[i + 1:i + n + 1]):
            fundos.append(i)
    return topos, fundos


def estrutura(df):
    """Tendência e recuo num gráfico (df com High, Low, Close)."""
    h, l, c = (df[k].to_numpy(float) for k in ("High", "Low", "Close"))
    if len(c) < 20:
        return None
    tp, fd = pivos(h, l)
    if len(tp) < 2 or len(fd) < 2:
        return {"tend": False, "recuo": False}
    t1, t2, f1, f2 = h[tp[-1]], h[tp[-2]], l[fd[-1]], l[fd[-2]]
    preco = c[-1]
    antes = [i for i in fd if i < tp[-1]]          # perna de alta: do fundo antes do último topo até o topo
    base = l[antes[-1]] if antes else f1
    pct = (t1 - preco) / (t1 - base) if t1 > base else None
    return {"tend": bool(t1 > t2 and f1 > f2 and preco > f1), "recuo": bool(f1 < preco < t1),
            "pct": None if pct is None else round(float(pct), 3),
            "topo": round(float(t1), 2), "topo_ant": round(float(t2), 2),
            "fundo": round(float(f1), 2), "fundo_ant": round(float(f2), 2),
            "dt_topo": str(df.index[tp[-1]].date()), "dt_fundo": str(df.index[fd[-1]].date())}


def analisar_precos(yahoo):
    out = {}
    for k in range(0, len(yahoo), 200):
        lote = yahoo[k:k + 200]
        try:
            d = yf.download(lote, period="2y", interval="1d", auto_adjust=True, progress=False,
                            group_by="ticker", threads=True)
        except Exception as e:
            print("  lote", k, "falhou:", e)
            continue
        for t in lote:
            try:
                x = (d[t] if isinstance(d.columns, pd.MultiIndex) else d).dropna()
            except KeyError:
                continue
            if len(x) < 60:
                continue
            sem = x.resample("W-FRI").agg({"Open": "first", "High": "max", "Low": "min", "Close": "last"}).dropna()
            out[t] = {"preco": round(float(x["Close"].iloc[-1]), 2),
                      "var1d": round(float(x["Close"].iloc[-1] / x["Close"].iloc[-2] - 1), 4),
                      "data": str(x.index[-1].date()),
                      "D": estrutura(x.tail(260)), "W": estrutura(sem.tail(110))}
        print(f"  preços {min(k + 200, len(yahoo))}/{len(yahoo)}", flush=True)
    return out


# ------------------------------------------------------------------ junta tudo
def lucro_ok(l):
    if not l or len(l["anos"]) < 3:
        return None                                # sem 3 anos de balanço: não dá para dizer
    return bool(all(v > 0 for _, v in l["anos"][:3]) and l["ttm"] is not None and l["ttm"] > 0)


def linha(r, sec, p):
    l = sec.get("lucro")
    cap = sec["acoes"] * p["preco"] / 1e9 if sec.get("acoes") else None
    return {"t": r["ticker"], "n": sec.get("nome") or r["nome"], "cap": None if cap is None else round(cap, 2),
            "setor": setor(sec.get("sic")), "ind": (sec.get("ind") or "").title(),
            "pais": sec.get("pais") or "Fora dos EUA",   # a SEC deixa sem descrição alguns endereços estrangeiros
            **p, "lucro": lucro_ok(l),
            "anos": [[e[:4], round(v / 1e6, 1)] for e, v in (l["anos"][:3] if l else [])],
            "ttm": None if not l else round(l["ttm"] / 1e6, 1), "base": l["base"] if l else None}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sec", action="store_true")
    ap.add_argument("--teste", nargs="*")
    a = ap.parse_args()
    univ = universo()
    print("ações da NYSE/Nasdaq/CBOE na SEC:", len(univ), flush=True)
    if a.teste:
        univ = [r for r in univ if r["ticker"] in a.teste]
    pr = analisar_precos([r["yahoo"] for r in univ])
    acoes = acoes_em_massa()
    print("nº de ações (consulta em massa):", len(acoes), "empresas", flush=True)
    lista = []
    for r in univ:                               # só busca na SEC quem tem preço e pode passar de US$ 300 mi
        p = pr.get(r["yahoo"])
        if not p:
            continue
        r["acoes"] = acoes.get(r["cik"])
        if r["acoes"] is None or r["acoes"] * p["preco"] >= CAP_MIN:
            lista.append(r)
    if a.teste:
        sec = {str(r["cik"]): {**perfil(r["cik"]), "lucro": lucros(r["cik"])} for r in lista}
    else:
        sec = atualizar_sec(lista, forcar=a.sec)
    linhas = []
    for r in lista:
        s = dict(sec.get(str(r["cik"])) or {})
        s["acoes"] = r["acoes"] or s.get("acoes")
        x = linha(r, s, pr[r["yahoo"]])
        if x["cap"] is not None and x["cap"] * 1e9 >= CAP_MIN:
            linhas.append(x)
    if a.teste:
        for x in linhas:
            print(json.dumps(x, ensure_ascii=False))
        return
    SITE.mkdir(exist_ok=True)
    meta = {"gerado": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "pregao": max(x["data"] for x in linhas), "n": len(linhas)}
    (SITE / "data.json").write_text(json.dumps({"meta": meta, "acoes": linhas}, ensure_ascii=False,
                                               separators=(",", ":")), encoding="utf-8")
    print("ok:", meta)


if __name__ == "__main__":
    main()
