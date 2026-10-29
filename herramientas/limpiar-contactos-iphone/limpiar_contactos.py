#!/usr/bin/env python3
"""
Limpia una exportación de contactos (.vcf) de iPhone / iCloud / Google.

- Elimina los contactos que no tienen ningún número telefónico.
- Unifica los contactos repetidos (mismo número o mismo nombre) en uno solo.
- Quita números repetidos dentro de un mismo contacto.
- Etiqueta los números de cada contacto como "Teléfono 1", "Teléfono 2", ...
- Con --estructurar separa del nombre el rango o título, el cargo y la empresa
  ("Ing. Edwin Arellano - Sub Gerente MARSA" -> Ing. | Edwin | Arellano |
  Sub Gerente | MARSA) y trata como corporativos los números que aparecen con
  nombres de personas distintas: el número queda en un solo contacto y los
  demás nombres pasan a sus notas.
- Genera un reporte CSV con lo que se unificó y lo que se eliminó.

Uso:
    python3 limpiar_contactos.py contactos.vcf
    python3 limpiar_contactos.py contactos.vcf -o limpios.vcf --conservar-extras --estructurar \\
        --organizaciones MARSA,ECL

Solo usa la biblioteca estándar de Python 3.8+.
"""

import argparse
import csv
import quopri
import re
import sys
import unicodedata
from collections import Counter, OrderedDict
from pathlib import Path

# Notas que agregan otras apps y no aportan nada.
NOTAS_AUTOMATICAS = re.compile(r"^(Este contacto es de solo lectura|Added by WhatsApp)", re.I)

# Siglas y nombres que se reconocen como empresa o entidad (se amplía con --organizaciones).
ORGANIZACIONES = {
    "PNP", "DIRCOTE", "DIVIAC", "OEFA", "SUNAFIL", "OSINERGMIN", "INGEMMET", "SUNAT", "SUNARP",
    "MP", "MPFN", "GREM", "MINEM", "MINAM", "SUCAMEC", "ESSALUD", "BCP", "BBVA", "INTERBANK",
    "PUCP", "UNFV", "UPC", "ULIMA", "UNMSM", "SODEXO", "LATAM", "DHL", "RPP",
    "CLARO", "ENTEL", "MOVISTAR", "BITEL",
}

# Rangos y títulos que van antes del nombre.
TITULOS = {
    "dr", "dra", "doctor", "doctora", "ing", "inge", "lic", "abg", "abog", "mg", "mag", "prof",
    "sr", "sra", "srta", "cmdte", "comandante", "mayor", "may", "cap", "capitan", "capitán",
    "crnel", "coronel", "gral", "general", "tte", "teniente", "alferez", "alférez", "suboficial",
    "so", "so1", "so2", "so3", "sot", "sot1", "sot2", "sot3", "s1", "s2", "s3", "perito", "fiscal",
    "efectivo",
}
TITULOS_COMPUESTOS = {("sub", "oficial"), ("sub", "teniente"), ("sub", "comisario")}

# Palabras que marcan al titular actual de un número y que no forman parte del nombre.
MARCAS_ACTUAL = {"actual", "new", "nuevo", "nueva"}

# Palabras que no identifican a una persona al comparar nombres.
GENERICAS = TITULOS | MARCAS_ACTUAL | {
    "de", "del", "la", "las", "los", "el", "y", "san", "santa",
    "mama", "papa", "tia", "tio", "primo", "prima", "hermano", "hermana", "amigo", "amiga",
    "abogado", "abogada", "asistente", "gerente", "jefe", "supervisor", "superintendente",
    "secretario", "secretaria", "mina", "oficina", "trabajo", "telf", "celular", "cel",
}

PARTICULAS = {"de", "del", "la", "las", "los", "y", "da", "van", "von"}

# Palabras que indican que el "nombre" describe a alguien o a un negocio y no se
# debe partir en nombres y apellidos ("Asesora Scotiabank Olguita", "Taxi Juan").
DESCRIPTORES = GENERICAS | {
    "asesor", "asesora", "recepcion", "taxi", "hotel", "chofer", "conductor", "repartidor",
    "delivery", "tienda", "salon", "autos", "restaurante", "clinica", "colegio", "universidad",
    "estudio", "notaria", "juzgado", "doctor", "medico", "vecino", "vecina", "hijo", "hija",
    "esposo", "esposa", "sobrino", "sobrina", "cuñado", "cunado", "cuñada", "cunada",
    "suegro", "suegra", "abuelo", "abuela", "senor", "senora", "profe", "profesor", "profesora",
    "cliente", "proveedor", "of", "oficina", "area", "dpto", "departamento", "centro",
}


# ---------------------------------------------------------------------------
# Lectura del vCard
# ---------------------------------------------------------------------------

def desplegar_lineas(texto):
    """Une las líneas plegadas (RFC 6350) y los saltos suaves de quoted-printable."""
    lineas = []
    for linea in texto.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if lineas and linea[:1] in (" ", "\t"):
            lineas[-1] += linea[1:]
        elif lineas and lineas[-1].endswith("=") and "QUOTED-PRINTABLE" in lineas[-1].upper().split(":", 1)[0]:
            lineas[-1] = lineas[-1][:-1] + linea
        else:
            lineas.append(linea)
    return lineas


def separar_propiedad(linea):
    """'item1.TEL;type=CELL:+51 9...' -> ('item1', 'TEL', {'TYPE': [...]}, '+51 9...')"""
    en_comillas = False
    corte = -1
    for i, c in enumerate(linea):
        if c == '"':
            en_comillas = not en_comillas
        elif c == ":" and not en_comillas:
            corte = i
            break
    if corte < 0:
        return None
    cabecera, valor = linea[:corte], linea[corte + 1:]
    partes = cabecera.split(";")
    nombre = partes[0]
    grupo = ""
    if "." in nombre:
        grupo, nombre = nombre.rsplit(".", 1)
    params = {}
    for p in partes[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            params.setdefault(k.upper(), []).extend(v.strip('"').split(","))
        else:
            params.setdefault("TYPE", []).append(p)
    return grupo, nombre.upper(), params, valor


def decodificar(params, valor):
    if any(v.upper() == "QUOTED-PRINTABLE" for v in params.get("ENCODING", [])):
        charset = (params.get("CHARSET") or ["utf-8"])[0]
        try:
            return quopri.decodestring(valor.encode("latin-1", "replace")).decode(charset, "replace")
        except LookupError:
            return quopri.decodestring(valor.encode("latin-1", "replace")).decode("utf-8", "replace")
    return valor


def desescapar(valor):
    return (valor.replace("\\n", "\n").replace("\\N", "\n")
            .replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\"))


def componentes(valor):
    return [desescapar(c).strip() for c in re.split(r"(?<!\\);", valor)]


def tarjeta_vacia():
    return {"FN": "", "N": "", "TEL": [], "EMAIL": [], "ADR": [], "URL": [], "NOTE": [],
            "NICKNAME": [], "BDAY": "", "ORG": [], "TITLE": "", "PHOTO": ""}


def leer_vcf(ruta):
    datos = Path(ruta).read_bytes()
    try:
        texto = datos.decode("utf-8-sig")
    except UnicodeDecodeError:
        texto = datos.decode("latin-1")

    tarjetas, actual = [], None
    for linea in desplegar_lineas(texto):
        if not linea.strip():
            continue
        mayus = linea.strip().upper()
        if mayus == "BEGIN:VCARD":
            actual = tarjeta_vacia()
            continue
        if mayus == "END:VCARD":
            if actual is not None:
                tarjetas.append(actual)
            actual = None
            continue
        if actual is None:
            continue
        prop = separar_propiedad(linea)
        if not prop:
            continue
        _grupo, nombre, params, valor = prop
        if nombre == "PHOTO":
            # Se guarda la línea original (sin el prefijo de grupo); se conserva la más grande.
            linea = linea.split(".", 1)[1] if "." in linea.split(":", 1)[0].split(";")[0] else linea
            if len(linea) > len(actual["PHOTO"]):
                actual["PHOTO"] = linea
            continue
        valor = decodificar(params, valor)
        if nombre == "FN":
            actual["FN"] = desescapar(valor).strip()
        elif nombre == "N":
            actual["N"] = valor
        elif nombre == "TEL":
            valor = valor.strip()
            if valor.lower().startswith("tel:"):
                valor = valor[4:]
            if re.search(r"\d", valor):
                actual["TEL"].append(valor)
        elif nombre == "EMAIL":
            actual["EMAIL"].append(valor.strip())
        elif nombre == "ADR":
            if any(componentes(valor)):
                actual["ADR"].append(valor)
        elif nombre == "URL":
            if not valor.lower().startswith("ms-outlook:"):
                actual["URL"].append(desescapar(valor).strip())
        elif nombre == "NOTE":
            nota = desescapar(valor).strip()
            if nota and not NOTAS_AUTOMATICAS.match(nota):
                actual["NOTE"].append(nota)
        elif nombre == "NICKNAME":
            actual["NICKNAME"].extend(x for x in componentes(valor.replace(",", ";")) if x)
        elif nombre == "BDAY":
            fecha = leer_cumpleanos(valor, params)
            if fecha and not actual["BDAY"]:
                actual["BDAY"] = fecha
        elif nombre == "ORG":
            actual["ORG"] = actual["ORG"] or [x for x in componentes(valor) if x]
        elif nombre == "TITLE":
            actual["TITLE"] = actual["TITLE"] or desescapar(valor).strip()
    return tarjetas


# ---------------------------------------------------------------------------
# Normalización
# ---------------------------------------------------------------------------

def leer_cumpleanos(valor, params):
    """Devuelve 'AAAA-MM-DD' o '--MM-DD' si no se conoce el año.
    Apple guarda el cumpleaños sin año como 1604 y Outlook como 1904."""
    m = re.match(r"^(\d{4}|--)-?(\d{2})-?(\d{2})", valor.strip())
    if not m:
        return ""
    anio, mes, dia = m.groups()
    if anio in ("--", "1604", "1900", "1904") or "X-APPLE-OMIT-YEAR" in params:
        return "--{}-{}".format(mes, dia)
    return "{}-{}-{}".format(anio, mes, dia)


def elegir_cumpleanos(fechas):
    """Entre las copias, la fecha con año; a igualdad, la que más se repite."""
    if not fechas:
        return ""
    frecuencia = Counter(fechas)
    return max(fechas, key=lambda f: (not f.startswith("--"), frecuencia[f]))


def linea_cumpleanos(fecha):
    if fecha.startswith("--"):
        return "BDAY;X-APPLE-OMIT-YEAR=1604:1604" + fecha[1:]
    return "BDAY:" + fecha


def nombre_desde_n(n):
    comps = componentes(n) + [""] * 5
    apellido, nombre, segundo, prefijo, sufijo = comps[:5]
    return " ".join(x for x in (prefijo, nombre, segundo, apellido, sufijo) if x)


def clave_nombre(nombre):
    s = unicodedata.normalize("NFKD", nombre).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    return s


def normalizar_telefono(crudo, pais):
    """Devuelve (clave_para_comparar, texto_para_mostrar)."""
    crudo = crudo.strip()
    digitos = re.sub(r"\D", "", crudo)
    internacional = crudo.startswith("+")
    if digitos.startswith("00"):
        digitos, internacional = digitos[2:], True

    if not internacional and pais == "51":
        if len(digitos) == 9 and digitos.startswith("9"):          # celular: 987654321
            digitos, internacional = "51" + digitos, True
        elif len(digitos) == 11 and digitos.startswith("519"):     # 51987654321 sin "+"
            internacional = True
        elif digitos.startswith("0") and 8 <= len(digitos) <= 9:   # fijo con código: 014567890
            digitos, internacional = "51" + digitos[1:], True

    if not internacional:
        return digitos, crudo

    if digitos.startswith("519") and len(digitos) == 11:
        mostrar = "+51 {} {} {}".format(digitos[2:5], digitos[5:8], digitos[8:])
    else:
        mostrar = "+" + digitos
    return digitos, mostrar


def palabras_clave(nombre, organizaciones):
    """Palabras que identifican a la persona (sin títulos, cargos ni empresas)."""
    return {w for w in clave_nombre(nombre).split()
            if len(w) >= 3 and w not in GENERICAS and w.upper() not in organizaciones and not w.isdigit()}


def misma_persona(a, b, organizaciones):
    """Dos nombres con el mismo número son de la misma persona si comparten alguna palabra."""
    pa, pb = palabras_clave(a, organizaciones), palabras_clave(b, organizaciones)
    if not pa or not pb:
        return True  # "Abogado MARSA", "Mamá": no basta para decir que es otra persona
    return any(x == y or (min(len(x), len(y)) >= 4 and (x.startswith(y) or y.startswith(x)))
               for x in pa for y in pb)


def tiene_marca_actual(nombre):
    return bool(set(clave_nombre(nombre).split()) & MARCAS_ACTUAL)


# ---------------------------------------------------------------------------
# Separación del nombre
# ---------------------------------------------------------------------------

def capitalizar(palabra):
    if palabra.islower() or (palabra.isupper() and len(palabra) > 3 and palabra.isalpha()):
        return palabra.lower() if palabra.lower() in PARTICULAS else palabra.capitalize()
    return palabra


def es_organizacion(palabra, organizaciones):
    limpia = re.sub(r"[^\wÁÉÍÓÚÑáéíóúñ]", "", palabra)
    if limpia.upper() not in organizaciones:
        return False
    # Las siglas cortas solo cuentan en mayúsculas ("MP", no "mp").
    return len(limpia) > 3 or limpia.isupper()


def parece_nombre(palabra):
    """Palabra que puede ser nombre o apellido: solo letras (admite guion y apóstrofo)."""
    return (re.fullmatch(r"[^\W\d_]+(?:[-'’][^\W\d_]+)*", palabra) is not None
            and clave_nombre(palabra) not in DESCRIPTORES)


def separar_organizacion(org, cargo, organizaciones):
    """ORG 'Chofer L-127 - MARSA' -> (['MARSA'], 'Chofer L-127')."""
    if len(org) != 1 or "/" in org[0]:
        return org, cargo
    palabras = org[0].replace(" - ", " ").split()
    siglas = [w for w in palabras if es_organizacion(w, organizaciones)]
    if not siglas or len(siglas) == len(palabras):
        return org, cargo
    resto = " ".join(w for w in palabras if w not in siglas)
    return [siglas[0].upper()], cargo or resto


def estructurar_nombre(nombre, organizaciones):
    """'Cmdte PNP Cesar Torres DIRCOTE Delta 4' ->
    {prefijo: 'Cmdte', nombres: 'Cesar', apellidos: 'Torres', empresa: ['PNP', 'DIRCOTE'], cargo: 'Delta 4'}"""
    r = {"prefijo": "", "nombres": "", "apellidos": "", "apodo": "", "empresa": [], "cargo": ""}

    apodo = re.search(r"[“\"«]([^”\"»]+)[”\"»]", nombre)
    if apodo:
        r["apodo"] = apodo.group(1).strip()
        nombre = nombre.replace(apodo.group(0), " ")

    partes = [p.strip() for p in re.split(r"\s+[-–]\s*|\s*[-–]\s+", nombre) if p.strip()]
    cabeza, colas = (partes[0] if partes else ""), partes[1:]

    def sin_marcas(texto):
        palabras = []
        for w in texto.split():
            partes = w.split("-")
            # "DIRCOTE-Delta" son dos palabras; "Guerra-Garcia" es un apellido.
            palabras += partes if any(es_organizacion(x, organizaciones) for x in partes) else [w]
        return [w for w in palabras if w and clave_nombre(w) not in MARCAS_ACTUAL]

    palabras = sin_marcas(cabeza)
    cargo = []
    for cola in colas:
        for w in sin_marcas(cola):
            if es_organizacion(w, organizaciones):
                r["empresa"].append(w.strip(".,").upper() if len(w) <= 6 else w.strip(".,"))
            else:
                cargo.append(w)

    # Rango o título al inicio ("Dr.", "Cmdte PNP", "Sub Oficial").
    prefijo = []
    while palabras:
        w = clave_nombre(palabras[0])
        if len(palabras) > 1 and (w, clave_nombre(palabras[1])) in TITULOS_COMPUESTOS:
            prefijo += palabras[:2]
            palabras = palabras[2:]
        elif w in TITULOS and len(palabras) > 1:
            prefijo.append(palabras.pop(0))
        elif prefijo and (es_organizacion(palabras[0], organizaciones) or palabras[0].upper() in organizaciones):
            r["empresa"].append(palabras.pop(0).upper())
        else:
            break

    # Empresa dentro del nombre: lo que sigue a la última sigla es la unidad o el cargo.
    indices = [i for i, w in enumerate(palabras) if es_organizacion(w, organizaciones)]
    if indices:
        r["empresa"] += [palabras[i].upper() if len(palabras[i]) <= 6 else palabras[i] for i in indices]
        cargo = palabras[indices[-1] + 1:] + cargo
        palabras = [w for i, w in enumerate(palabras[:indices[-1]]) if i not in indices]

    r["prefijo"] = " ".join(prefijo)
    # Sin palabras repetidas seguidas ("Operador SSPP SSPP").
    r["cargo"] = " ".join(w for i, w in enumerate(cargo) if i == 0 or w.lower() != cargo[i - 1].lower())
    r["empresa"] = list(OrderedDict.fromkeys(r["empresa"]))
    palabras = [capitalizar(w) for w in palabras]

    # Nombres y apellidos: 2 -> 1+1, 3 -> 1+2, 4 -> 2+2. Con más palabras, o con
    # números, no es un nombre de persona y se deja entero.
    persona = [w for w in palabras if w.lower() not in PARTICULAS]
    if prefijo and len(persona) == 1:
        # "Cmdte Torres", "SO Flores": tras un rango suele ir el apellido.
        r["apellidos"] = " ".join(palabras)
    elif 2 <= len(persona) <= 4 and all(parece_nombre(w) for w in persona):
        n_nombres = 2 if len(persona) == 4 else 1
        cuenta, corte = 0, len(palabras)
        for i, w in enumerate(palabras):
            if w.lower() not in PARTICULAS:
                cuenta += 1
                if cuenta == n_nombres:
                    corte = i + 1
                    break
        r["nombres"] = " ".join(palabras[:corte])
        r["apellidos"] = " ".join(palabras[corte:])
    else:
        r["nombres"] = " ".join(palabras)
    return r


# ---------------------------------------------------------------------------
# Unificación
# ---------------------------------------------------------------------------

class Conjuntos:
    def __init__(self, n):
        self.padre = list(range(n))

    def raiz(self, i):
        while self.padre[i] != i:
            self.padre[i] = self.padre[self.padre[i]]
            i = self.padre[i]
        return i

    def unir(self, a, b):
        ra, rb = self.raiz(a), self.raiz(b)
        if ra != rb:
            self.padre[rb] = ra


def completitud(nombre):
    return len([w for w in clave_nombre(nombre).split() if w not in MARCAS_ACTUAL])


def mejor_nombre(nombres):
    if not nombres:
        return ""
    frecuencia = Counter(clave_nombre(n) for n in nombres)
    return max(nombres, key=lambda n: (completitud(n), frecuencia[clave_nombre(n)], len(n)))


def limpiar(tarjetas, pais, unir_por_nombre, corporativos=False, organizaciones=frozenset()):
    con_tel, sin_tel = [], []
    for t in tarjetas:
        nombre = t["FN"] or nombre_desde_n(t["N"]) or (" ".join(t["ORG"]))
        t["nombre"] = nombre.strip()
        t["tels"] = [normalizar_telefono(x, pais) for x in t["TEL"]]
        t["tels"] = [x for x in t["tels"] if x[0]]
        (con_tel if t["tels"] else sin_tel).append(t)

    def es_numero(nombre):
        return not clave_nombre(nombre).replace(" ", "").isalpha()

    cj = Conjuntos(len(con_tel))
    por_tel, vistos_nombre = OrderedDict(), {}
    for i, t in enumerate(con_tel):
        for clave, _ in t["tels"]:
            por_tel.setdefault(clave, []).append(i)
        kn = clave_nombre(t["nombre"])
        if corporativos and t["nombre"]:
            # "Alcides Mantari - Jefe" y "Alcides Mantari" son la misma persona.
            e = estructurar_nombre(t["nombre"], organizaciones)
            kn = clave_nombre(e["nombres"] + " " + e["apellidos"]) or kn
        # Un nombre de una sola palabra ("Ana", "Mamá") puede ser de personas distintas.
        if unir_por_nombre and len(kn.split()) >= 2 and not kn.replace(" ", "").isdigit():
            if kn in vistos_nombre:
                cj.unir(vistos_nombre[kn], i)
            else:
                vistos_nombre[kn] = i

    for indices in por_tel.values():
        for i in indices[1:]:
            a, b = con_tel[indices[0]]["nombre"], con_tel[i]["nombre"]
            if not corporativos or not a or not b or es_numero(a) or es_numero(b):
                cj.unir(indices[0], i)
                continue
            # Con números corporativos, solo se une con quien parezca la misma persona.
            for j in indices[:indices.index(i)]:
                if misma_persona(con_tel[j]["nombre"], con_tel[i]["nombre"], organizaciones):
                    cj.unir(j, i)
                    break

    grupos = OrderedDict()
    for i, t in enumerate(con_tel):
        grupos.setdefault(cj.raiz(i), []).append(t)
    grupos = list(grupos.values())

    # Número que quedó en contactos de personas distintas: se lo queda el que dice
    # "Actual"/"New"; si ninguno, el de nombre más completo. Los demás nombres van a notas.
    otros_nombres = [[] for _ in grupos]
    quitar = [set() for _ in grupos]
    if corporativos:
        duenos = OrderedDict()
        for g, miembros in enumerate(grupos):
            for m in miembros:
                for clave, _ in m["tels"]:
                    duenos.setdefault(clave, OrderedDict())[g] = True
        for clave, gs in duenos.items():
            if len(gs) < 2:
                continue

            def puntaje(g):
                nombres = [m["nombre"] for m in grupos[g]]
                return (any(tiene_marca_actual(n) for n in nombres), completitud(mejor_nombre(nombres)), len(nombres))

            ganador = max(gs, key=puntaje)
            for g in gs:
                if g != ganador:
                    quitar[g].add(clave)
                    nombre = mejor_nombre([m["nombre"] for m in grupos[g]])
                    if nombre and nombre not in otros_nombres[ganador]:
                        otros_nombres[ganador].append(nombre)

    resultado, absorbidos = [], []
    for g, miembros in enumerate(grupos):
        nombres = [m["nombre"] for m in miembros if m["nombre"]]
        nombre = mejor_nombre(nombres)

        telefonos = OrderedDict()
        for m in miembros:
            for clave, mostrar in m["tels"]:
                if clave in quitar[g]:
                    continue
                if clave not in telefonos or (mostrar.startswith("+") and not telefonos[clave].startswith("+")):
                    telefonos[clave] = mostrar
        if not telefonos:
            absorbidos.append({"nombre": nombre, "originales": nombres, "cantidad": len(miembros)})
            continue

        def unicos(campo, clave=lambda x: x.lower()):
            vistos = OrderedDict()
            for m in miembros:
                for v in m[campo]:
                    vistos.setdefault(clave(v), v)
            return list(vistos.values())

        notas = unicos("NOTE")
        if otros_nombres[g]:
            notas.append("Otros nombres con este número: " + ", ".join(otros_nombres[g]))

        base = next((m for m in miembros if m["nombre"] == nombre), miembros[0])
        resultado.append({
            "nombre": nombre or next(iter(telefonos.values())),
            "N": base["N"],
            "FN_original": base["FN"],
            "telefonos": list(telefonos.values()),
            "EMAIL": unicos("EMAIL"),
            "ADR": unicos("ADR", lambda x: clave_nombre(x)),
            "URL": unicos("URL"),
            "NICKNAME": unicos("NICKNAME"),
            "NOTE": notas,
            "BDAY": elegir_cumpleanos([m["BDAY"] for m in miembros if m["BDAY"]]),
            "ORG": next((m["ORG"] for m in miembros if m["ORG"]), []),
            "TITLE": next((m["TITLE"] for m in miembros if m["TITLE"]), ""),
            "PHOTO": max((m["PHOTO"] for m in miembros), key=len),
            "originales": nombres,
            "otros_nombres": otros_nombres[g],
            "cantidad": len(miembros),
        })

    resultado.sort(key=lambda c: clave_nombre(c["nombre"]))
    return resultado, sin_tel, absorbidos


# ---------------------------------------------------------------------------
# Escritura
# ---------------------------------------------------------------------------

def escapar(valor):
    return (valor.replace("\\", "\\\\").replace("\n", "\\n")
            .replace(",", "\\,").replace(";", "\\;"))


def plegar(linea, limite=75):
    """Pliega una línea a 75 octetos sin partir caracteres UTF-8."""
    salida, actual, tam = [], "", 0
    for c in linea:
        b = len(c.encode("utf-8"))
        if tam + b > limite:
            salida.append(actual)
            actual, tam, limite = " ", 1, 75
        actual += c
        tam += b
    salida.append(actual)
    return "\r\n".join(salida)


def campos_nombre(c, estructurar, organizaciones):
    """Devuelve (N, FN, apodos, empresa, cargo) del contacto."""
    apodos, empresa, cargo = list(c["NICKNAME"]), list(c["ORG"]), c["TITLE"]
    n = componentes(c["N"]) + [""] * 5 if c["N"] else [""] * 5
    estructurado = (n[0] and n[1] and c["FN_original"] == nombre_desde_n(c["N"])
                    and all(parece_nombre(w) or w.lower() in PARTICULAS for w in " ".join(n[:3]).split()))
    if estructurar:
        empresa, cargo = separar_organizacion(empresa, cargo, organizaciones)
    if not estructurar or estructurado or not c["nombre"] or c["nombre"].startswith("+"):
        if not any(n):
            n = ["", c["nombre"], "", "", ""]
        fn = c["nombre"] if (not estructurar or not estructurado) else nombre_desde_n(c["N"])
        return n[:5], fn, apodos, empresa, cargo

    e = estructurar_nombre(c["nombre"], organizaciones)
    if e["apodo"] and e["apodo"] not in apodos:
        apodos.insert(0, e["apodo"])
    empresa = empresa or e["empresa"]
    cargo = cargo or e["cargo"]
    n = [e["apellidos"], e["nombres"], "", e["prefijo"], ""]
    fn = " ".join(x for x in (e["prefijo"], e["nombres"], e["apellidos"]) if x)
    return n, fn or " ".join(empresa), apodos, empresa, cargo


def escribir_vcf(contactos, ruta, conservar_extras, estructurar=False, organizaciones=frozenset()):
    with open(ruta, "w", encoding="utf-8", newline="") as f:
        for c in contactos:
            n, fn, apodos, empresa, cargo = campos_nombre(c, estructurar, organizaciones)
            lineas = ["BEGIN:VCARD", "VERSION:3.0",
                      "N:" + ";".join(escapar(x) for x in n),
                      "FN:" + escapar(fn)]
            if estructurar and not n[0] and not n[1] and empresa:
                lineas.append("X-ABShowAs:COMPANY")
            for i, tel in enumerate(c["telefonos"], 1):
                lineas.append("item{}.TEL;type=VOICE:{}".format(i, tel))
                lineas.append("item{}.X-ABLabel:Teléfono {}".format(i, i))
            if conservar_extras or estructurar:
                if apodos:
                    lineas.append("NICKNAME:" + ",".join(escapar(x) for x in apodos))
                if empresa:
                    lineas.append("ORG:" + ";".join(escapar(x) for x in empresa))
                if cargo:
                    lineas.append("TITLE:" + escapar(cargo))
                if estructurar and empresa:
                    lineas.append("CATEGORIES:" + escapar(empresa[0]))
            if conservar_extras:
                lineas += ["EMAIL;type=INTERNET:" + x for x in c["EMAIL"]]
                lineas += ["ADR;type=HOME:" + x for x in c["ADR"]]
                if c["BDAY"]:
                    lineas.append(linea_cumpleanos(c["BDAY"]))
                lineas += ["URL:" + escapar(x) for x in c["URL"]]
                if c["PHOTO"]:
                    lineas.append(c["PHOTO"])
            if c["NOTE"] and (conservar_extras or c["otros_nombres"]):
                lineas.append("NOTE:" + escapar("\n".join(c["NOTE"] if conservar_extras else c["NOTE"][-1:])))
            lineas.append("END:VCARD")
            f.write("".join(plegar(l) + "\r\n" for l in lineas))


def escribir_reporte(contactos, sin_tel, absorbidos, ruta):
    with open(ruta, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["accion", "nombre_final", "contactos_unificados", "nombres_originales", "telefonos",
                    "otros_nombres_con_el_numero"])
        for c in contactos:
            accion = "unificado" if c["cantidad"] > 1 else "conservado"
            w.writerow([accion, c["nombre"], c["cantidad"], " | ".join(c["originales"]),
                        " | ".join(c["telefonos"]), " | ".join(c["otros_nombres"])])
        for a in absorbidos:
            w.writerow(["numero_corporativo_reasignado", a["nombre"], a["cantidad"], " | ".join(a["originales"]), "", ""])
        for t in sin_tel:
            w.writerow(["eliminado_sin_telefono", t["nombre"] or "(vacío)", 1, t["nombre"], "", ""])


def main():
    ap = argparse.ArgumentParser(description="Limpia y unifica contactos de iPhone (.vcf).")
    ap.add_argument("entrada", help="Archivo .vcf exportado desde iCloud o el iPhone")
    ap.add_argument("-o", "--salida", help="Archivo .vcf limpio (por defecto: <entrada>_limpio.vcf)")
    ap.add_argument("--pais", default="51", help="Código de país para números sin prefijo (por defecto 51 = Perú)")
    ap.add_argument("--conservar-extras", action="store_true",
                    help="Conserva apodo, correo, empresa, cargo, cumpleaños, notas, dirección, web y foto")
    ap.add_argument("--no-unir-por-nombre", action="store_true",
                    help="Unifica solo por número repetido, no por nombre idéntico")
    ap.add_argument("--estructurar", action="store_true",
                    help="Separa título, nombres, apellidos, cargo y empresa, y trata los números "
                         "compartidos por personas distintas como corporativos")
    ap.add_argument("--organizaciones", default="",
                    help="Siglas o nombres de empresas adicionales, separados por coma (ej. MARSA,ECL)")
    args = ap.parse_args()

    organizaciones = frozenset(ORGANIZACIONES | {x.strip().upper() for x in args.organizaciones.split(",") if x.strip()})
    entrada = Path(args.entrada)
    salida = Path(args.salida) if args.salida else entrada.with_name(entrada.stem + "_limpio.vcf")
    reporte = salida.with_name(salida.stem + "_reporte.csv")

    tarjetas = leer_vcf(entrada)
    if not tarjetas:
        sys.exit("No se encontraron contactos en " + str(entrada))
    contactos, sin_tel, absorbidos = limpiar(tarjetas, args.pais, not args.no_unir_por_nombre,
                                             args.estructurar, organizaciones)
    escribir_vcf(contactos, salida, args.conservar_extras, args.estructurar, organizaciones)
    escribir_reporte(contactos, sin_tel, absorbidos, reporte)

    unificados = sum(1 for c in contactos if c["cantidad"] > 1)
    duplicados = sum(c["cantidad"] - 1 for c in contactos)
    print("Contactos leídos:                 {}".format(len(tarjetas)))
    print("Eliminados (sin teléfono/vacíos): {}".format(len(sin_tel)))
    print("Duplicados absorbidos:            {} (en {} contactos unificados)".format(duplicados, unificados))
    if args.estructurar:
        print("Números corporativos resueltos:   {} contactos con otros nombres en notas; {} titulares anteriores sin otro número".format(
            sum(1 for c in contactos if c["otros_nombres"]), len(absorbidos)))
    print("Contactos finales:                {}".format(len(contactos)))
    print("Archivo limpio:  {}".format(salida))
    print("Reporte:         {}".format(reporte))


if __name__ == "__main__":
    main()
