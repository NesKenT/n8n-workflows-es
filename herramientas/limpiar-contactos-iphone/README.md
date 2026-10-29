# Limpiar contactos del iPhone

Script que toma tus contactos exportados desde iCloud (`.vcf`) y:

- **Elimina** los contactos vacíos y los que no tienen número telefónico.
- **Unifica** en uno solo los contactos repetidos (mismo número o mismo nombre).
- **Quita** números repetidos dentro de un contacto (`987654321`, `+51 987 654 321` y `0051987654321` cuentan como el mismo).
- **Etiqueta** los números de cada contacto como `Teléfono 1`, `Teléfono 2`, etc.
- Agrega `+51` a los celulares peruanos de 9 dígitos guardados sin prefijo.
- Genera un **reporte CSV** con lo que se unificó y lo que se eliminó, para revisarlo antes de importar.

## Paso a paso

1. **Exporta tus contactos (respaldo)**: en una computadora entra a <https://www.icloud.com/contacts>,
   selecciona todos (`Ctrl/Cmd + A`) → menú `…` → **Exportar vCard**. Guarda ese archivo: es tu respaldo.
2. **Ejecuta el script** (requiere Python 3.8+, sin instalar nada más):

   ```bash
   python3 limpiar_contactos.py contactos.vcf
   ```

   Opciones:

   | Opción | Efecto |
   | --- | --- |
   | `--conservar-extras` | Conserva apodo, correo, empresa, cargo, cumpleaños, notas, dirección, web y foto (por defecto solo nombre + teléfonos). Las notas automáticas de Outlook y WhatsApp se descartan. |
   | `--estructurar` | Separa del nombre el rango o título, nombres, apellidos, cargo y empresa (`Cmdte PNP Cesar Torres DIRCOTE Delta 4` → *Cmdte* · *Cesar* · *Torres* · PNP/DIRCOTE · *Delta 4*). Además trata como corporativos los números guardados con nombres de personas distintas: el número queda en el contacto que diga "Actual"/"New" (o, si ninguno, en el de nombre más completo) y los otros nombres pasan a sus notas. |
   | `--organizaciones MARSA,ECL` | Siglas o empresas adicionales a reconocer dentro de los nombres. |
   | `--no-unir-por-nombre` | Unifica solo cuando se repite un número, no por nombre idéntico. |
   | `--pais 51` | Código de país para números sin prefijo (por defecto Perú). |
   | `-o salida.vcf` | Nombre del archivo de salida. |

3. **Revisa** `contactos_limpio_reporte.csv` (se abre en Excel). Fíjate especialmente en las filas `unificado`.
4. **Borra los contactos actuales** en icloud.com/contacts (seleccionar todos → Eliminar).
   Si además sincronizas contactos de Gmail/Outlook en el iPhone, desactívalos en
   *Ajustes → Contactos → Cuentas* para que no vuelvan a duplicarse.
5. **Importa** `contactos_limpio.vcf` en icloud.com/contacts (`…` → **Importar vCard**). Se sincroniza solo con el iPhone.

## Si la búsqueda de contactos es lenta o no encuentra

Suele deberse a muchos contactos duplicados o a varias cuentas sincronizando a la vez. Después de la limpieza:

- *Ajustes → Contactos → Cuentas*: deja una sola cuenta con contactos activada.
- *Ajustes → Apps → Contactos → Siri y Búsqueda*: desactiva y vuelve a activar
  "Mostrar app en Búsqueda" y "Mostrar contenido en Búsqueda" para reconstruir el índice.
- Reinicia el iPhone y déjalo cargando con Wi‑Fi unos minutos para que termine de indexar.
