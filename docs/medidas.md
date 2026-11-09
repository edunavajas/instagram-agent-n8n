# Medidas (Fase 3, sin Meta)

Fecha: 2026-11-09. Servidor del episodio: 4 vCPU y 16 GB de RAM, sin swap. Corren a la vez el ep2 (RAG, Outline y embeddings),
el ep3 (correo) y el ep4 (Instagram), sobre n8n 2.10.2 con task runners externos. Datos crudos en el servidor:
`logs/ep4-medidas-fase3.md`. CPU en % de un núcleo (100 % = 1 vCPU).

## Reposo

| Momento | RAM usada en el host | n8n | task-runners | n8n-db | knowledge-db |
|---|---|---|---|---|---|
| Antes de montar el ep4 | 3.644 MB (12.347 MB disponibles) | 359 MiB · 1 % | 7 MiB | 37 MiB | 71 MiB |
| Con el ep4 montado (9 flujos publicados) | 3.443 MB | 303 MiB · 0-2 % | 9 MiB | 44 MiB | 78 MiB |
| Al final, tras las pruebas y la limpieza | 3.670 MB | 303 MiB · 0,5 % | 98 MiB | 68 MiB | 75 MiB |

En reposo el ep4 no cuesta nada que se note: `IG 02b` corre cada 5 minutos (unos 0,5-1 s) y `IG 07` una vez al día.
Los runners se apagan tras 15 s sin trabajo, por eso su RAM sube y baja.

## Ráfaga de 50 comentarios con palabra clave

50 POST firmados, 10 en paralelo, por HTTPS público. Cada uno acaba en una respuesta privada y una pública (DRY_RUN).

| | Pasada 1 (muestreo cada 1 s) | Pasada 2 (`todos`) |
|---|---|---|
| Respuestas | 50 × 200 | 50 × 200 |
| Duración total de la ráfaga | 13,8 s | 13,6 s |
| Respuesta del webhook (lo que espera Meta) | p50 2,48 s · máx. 4,21 s | p50 2,31 s · máx. 4,56 s |
| Hasta el último comentario `respondido` | 14,4 s desde el primer POST | — |
| Comentarios procesados | 50, cada uno una vez, 0 errores | 50, 0 errores |

Durante la ráfaga (14 s con actividad):

| Contenedor | CPU pico | CPU media | RAM pico |
|---|---|---|---|
| n8n | 82 % | 63 % | 634 MiB (en reposo, ~300) |
| task-runners | 284 % | 239 % | 430 MiB |
| n8n-db | 25 % | 18 % | 66 MiB |
| knowledge-db | 8 % | 3 % | 87 MiB |
| Host | load1 pico 1,4 | | 4.161 MB usados (+~720 MB) |

El cuello de botella es el Code node de Python que verifica la firma: el runner de Python arranca un proceso por tarea
(~0,5 s) y atiende 5 a la vez (`N8N_RUNNERS_MAX_CONCURRENCY`). Ver `docs/spike-firma.md`.

## DM con el modelo (DeepSeek `deepseek-flash`, sin thinking)

13 DM con modelo: de media 2.091 tokens de entrada y 116 de salida. El modelo tarda 1,47 s de media (máx. 1,75 s).
El flujo `IG 03` entero, con el RAG incluido, tarda 1,5-2,1 s. El RAG (`02 - Buscar`) tarda 40-80 ms.
