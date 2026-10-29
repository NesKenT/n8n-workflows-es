#!/usr/bin/env python3
"""
Limpia una exportación de contactos (.vcf) de iPhone / iCloud.

- Elimina los contactos que no tienen ningún número telefónico.
- Unifica los contactos repetidos (mismo número o mismo nombre) en uno solo.
- Quita números repetidos dentro de un mismo contacto.
- Etiqueta los números de cada contacto como "Teléfono 1", "Teléfono 2", ...
- Genera un reporte CSV con lo que se unificó y lo que se eliminó.

Uso:
    python3 limpiar_contactos.py contactos.vcf
    python3 limpiar_contactos.py contactos.vcf -o limpios.vcf --conservar-extras

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

EXTRAS = ("ORG", "TITLE", "EMAIL", "BDAY", "NOTE", "ADR", "URL", "PHOTO")


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
            actual = {"FN": "", "N": "", "TEL": [], "extras": [], "raw_extras": []}
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
        if nombre == "FN":
            actual["FN"] = desescapar(decodificar(params, valor)).strip()
        elif nombre == "N":
            actual["N"] = decodificar(params, valor)
        elif nombre == "TEL":
            valor = decodificar(params, valor).strip()
            if valor.lower().startswith("tel:"):
                valor = valor[4:]
            if re.search(r"\d", valor):
                actual["TEL"].append(valor)
        elif nombre in EXTRAS:
            # Se guarda la línea original (sin el prefijo de grupo) para reescribirla tal cual.
            cabecera = linea.split(":", 1)[0]
            if "." in cabecera.split(";")[0]:
                linea = linea.split(".", 1)[1]
            actual["extras"].append((nombre, linea))
    return tarjetas


# ---------------------------------------------------------------------------
# Normalización
# ---------------------------------------------------------------------------

def nombre_desde_n(n):
    comps = [desescapar(c).strip() for c in re.split(r"(?<!\\);", n)] + [""] * 5
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


def limpiar(tarjetas, pais, unir_por_nombre):
    con_tel, sin_tel = [], []
    for t in tarjetas:
        nombre = t["FN"] or nombre_desde_n(t["N"])
        t["nombre"] = nombre.strip()
        t["tels"] = [normalizar_telefono(x, pais) for x in t["TEL"]]
        t["tels"] = [x for x in t["tels"] if x[0]]
        (con_tel if t["tels"] else sin_tel).append(t)

    cj = Conjuntos(len(con_tel))
    vistos_tel, vistos_nombre = {}, {}
    for i, t in enumerate(con_tel):
        for clave, _ in t["tels"]:
            if clave in vistos_tel:
                cj.unir(vistos_tel[clave], i)
            else:
                vistos_tel[clave] = i
        kn = clave_nombre(t["nombre"])
        # Un nombre de una sola palabra ("Ana", "Mamá") puede ser de personas distintas.
        if unir_por_nombre and len(kn.split()) >= 2 and not kn.replace(" ", "").isdigit():
            if kn in vistos_nombre:
                cj.unir(vistos_nombre[kn], i)
            else:
                vistos_nombre[kn] = i

    grupos = OrderedDict()
    for i, t in enumerate(con_tel):
        grupos.setdefault(cj.raiz(i), []).append(t)

    resultado = []
    for miembros in grupos.values():
        nombres = [m["nombre"] for m in miembros if m["nombre"]]
        if nombres:
            frecuencia = Counter(clave_nombre(n) for n in nombres)
            # El nombre más completo; a igualdad, el que más se repite.
            nombre = max(nombres, key=lambda n: (len(clave_nombre(n).split()), frecuencia[clave_nombre(n)], len(n)))
        else:
            nombre = ""
        base = next((m for m in miembros if m["nombre"] == nombre and m["N"]), None)

        telefonos = OrderedDict()
        for m in miembros:
            for clave, mostrar in m["tels"]:
                if clave not in telefonos or (mostrar.startswith("+") and not telefonos[clave].startswith("+")):
                    telefonos[clave] = mostrar

        extras, vistos = [], set()
        for m in miembros:
            for prop, linea in m["extras"]:
                unico = prop in ("ORG", "TITLE", "BDAY", "NOTE", "PHOTO")
                clave = prop if unico else linea.split(":", 1)[-1].strip().lower()
                if clave in vistos:
                    continue
                vistos.add(clave)
                extras.append(linea)

        resultado.append({
            "nombre": nombre or next(iter(telefonos.values())),
            "N": base["N"] if base else "",
            "telefonos": list(telefonos.values()),
            "extras": extras,
            "originales": nombres,
            "cantidad": len(miembros),
        })

    resultado.sort(key=lambda c: clave_nombre(c["nombre"]))
    return resultado, sin_tel


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


def escribir_vcf(contactos, ruta, conservar_extras):
    with open(ruta, "w", encoding="utf-8", newline="") as f:
        for c in contactos:
            lineas = ["BEGIN:VCARD", "VERSION:3.0"]
            if c["N"]:
                lineas.append("N:" + c["N"])
            else:
                lineas.append("N:;" + escapar(c["nombre"]) + ";;;")
            lineas.append("FN:" + escapar(c["nombre"]))
            for i, tel in enumerate(c["telefonos"], 1):
                lineas.append("item{}.TEL;type=VOICE:{}".format(i, tel))
                lineas.append("item{}.X-ABLabel:Teléfono {}".format(i, i))
            if conservar_extras:
                lineas.extend(c["extras"])
            lineas.append("END:VCARD")
            f.write("".join(plegar(l) + "\r\n" for l in lineas))


def escribir_reporte(contactos, sin_tel, ruta):
    with open(ruta, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["accion", "nombre_final", "contactos_unificados", "nombres_originales", "telefonos"])
        for c in contactos:
            accion = "unificado" if c["cantidad"] > 1 else "conservado"
            w.writerow([accion, c["nombre"], c["cantidad"], " | ".join(c["originales"]), " | ".join(c["telefonos"])])
        for t in sin_tel:
            w.writerow(["eliminado_sin_telefono", t["nombre"] or "(vacío)", 1, t["nombre"], ""])


def main():
    ap = argparse.ArgumentParser(description="Limpia y unifica contactos de iPhone (.vcf).")
    ap.add_argument("entrada", help="Archivo .vcf exportado desde iCloud")
    ap.add_argument("-o", "--salida", help="Archivo .vcf limpio (por defecto: <entrada>_limpio.vcf)")
    ap.add_argument("--pais", default="51", help="Código de país para números sin prefijo (por defecto 51 = Perú)")
    ap.add_argument("--conservar-extras", action="store_true",
                    help="Conserva correo, empresa, cumpleaños, notas, dirección y foto")
    ap.add_argument("--no-unir-por-nombre", action="store_true",
                    help="Unifica solo por número repetido, no por nombre idéntico")
    args = ap.parse_args()

    entrada = Path(args.entrada)
    salida = Path(args.salida) if args.salida else entrada.with_name(entrada.stem + "_limpio.vcf")
    reporte = salida.with_name(salida.stem + "_reporte.csv")

    tarjetas = leer_vcf(entrada)
    if not tarjetas:
        sys.exit("No se encontraron contactos en " + str(entrada))
    contactos, sin_tel = limpiar(tarjetas, args.pais, not args.no_unir_por_nombre)
    escribir_vcf(contactos, salida, args.conservar_extras)
    escribir_reporte(contactos, sin_tel, reporte)

    unificados = sum(1 for c in contactos if c["cantidad"] > 1)
    absorbidos = sum(c["cantidad"] - 1 for c in contactos)
    print("Contactos leídos:                 {}".format(len(tarjetas)))
    print("Eliminados (sin teléfono/vacíos): {}".format(len(sin_tel)))
    print("Duplicados absorbidos:            {} (en {} contactos unificados)".format(absorbidos, unificados))
    print("Contactos finales:                {}".format(len(contactos)))
    print("Archivo limpio:  {}".format(salida))
    print("Reporte:         {}".format(reporte))


if __name__ == "__main__":
    main()
