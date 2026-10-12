# Portfolio MCP — Salkantay Ventures (v3, nube + local)

Un mismo `server.py` sirve para dos modos:

- **Local (Claude Desktop)**: stdio, sin autenticación, igual que antes. No cambia nada de cómo lo usas hoy.
- **Nube (Cloud Run + conector remoto en claude.ai)**: HTTP con OAuth 2.1 propio (registro de cliente, login con usuario/contraseña, tokens), para poder usarlo desde claude.ai web o móvil sin depender de tu compu.

El modo se elige solo con variables de entorno — el mismo código, sin ramas que mantener por separado.

---

## Parte A — Uso local (si solo quieres seguir usándolo en Desktop)

Nada cambia respecto a la v2: reemplaza `server.py` y deja `credentials.json` como está. No necesitas nada de la Parte B.

---

## Parte B — Desplegar en la nube (Cloud Run)

### Requisitos

- Tener el [SDK de gcloud](https://cloud.google.com/sdk/docs/install) instalado y autenticado (`gcloud auth login`)
- El mismo proyecto de GCP donde ya creaste la Service Account (`salkantay-portfolio-mcp`)
- Docker no es necesario instalarlo — Cloud Run puede construir la imagen por ti con `gcloud run deploy --source .`

### Paso 1 — Elegir tus credenciales de acceso

Vas a necesitar un usuario y contraseña que usarás **tú** para aprobar el acceso cuando conectes claude.ai (no son las credenciales de Google, son solo para este login). Genera una contraseña fuerte:

```bash
openssl rand -base64 24
```

Guárdala — la vas a usar en el Paso 3 y cada vez que conectes un cliente nuevo en claude.ai.

### Paso 2 — Primer deploy (para obtener la URL pública)

Desde la carpeta `portfolio-mcp/` (con `server.py`, `requirements.txt`, `Dockerfile`, `.dockerignore` y tu `credentials.json` real, el mismo que ya usas local):

```bash
gcloud run deploy portfolio-mcp \
  --source . \
  --region us-central1 \
  --allow-unauthenticated \
  --set-env-vars MCP_AUTH_USERNAME=admin,MCP_AUTH_PASSWORD="TU_CONTRASEÑA_DEL_PASO_1" \
  --set-secrets GOOGLE_CREDENTIALS_JSON=portfolio-mcp-credentials:latest
```

Antes de correr esto, sube el `credentials.json` como secreto (una sola vez):

```bash
gcloud secrets create portfolio-mcp-credentials --data-file=credentials.json
```

> `--allow-unauthenticated` es sobre el acceso a nivel de Cloud Run (quién puede llegar al contenedor), no sobre tus datos — la autenticación real es el OAuth que ya implementamos. Sin este flag, Cloud Run pediría *además* un token de Google IAM y complicaría innecesariamente la conexión desde claude.ai.

Al terminar, el comando imprime algo como:

```
Service URL: https://portfolio-mcp-abc123xyz-uc.a.run.app
```

Copia esa URL — todavía el login OAuth no funciona bien porque el servidor no la conoce (ver el aviso "MCP_PUBLIC_URL no está definida" en los logs). Eso se resuelve en el paso 3.

### Paso 3 — Segundo deploy (con la URL real)

```bash
gcloud run deploy portfolio-mcp \
  --source . \
  --region us-central1 \
  --allow-unauthenticated \
  --set-env-vars MCP_AUTH_USERNAME=admin,MCP_AUTH_PASSWORD="TU_CONTRASEÑA_DEL_PASO_1",MCP_PUBLIC_URL="https://portfolio-mcp-abc123xyz-uc.a.run.app" \
  --set-secrets GOOGLE_CREDENTIALS_JSON=portfolio-mcp-credentials:latest
```

(Reemplaza la URL por la que te dio el Paso 2. Redeploys futuros del código no necesitan repetir este paso — la URL de Cloud Run no cambia mientras no borres el servicio.)

### Paso 4 — Conectar en claude.ai

1. En claude.ai: **Configuración → Connectors → Add custom connector**
2. Pega la URL del servicio (la del Paso 2/3)
3. Click "Add" — te va a redirigir a la página de login que armamos (tu usuario/contraseña del Paso 1)
4. Apruebas el acceso una vez, y queda conectado

---

## Parte C — Nube sin clave descargada (recomendado)

Si no hay `GOOGLE_CREDENTIALS_JSON` ni `credentials.json`, el servidor usa las credenciales por defecto de Google. En Cloud Run eso es la Service Account con la que corre el servicio, así que no hace falta descargar ni guardar ninguna clave:

```bash
gcloud run deploy portfolio-mcp \
  --source . \
  --region us-central1 \
  --service-account portfolio-mcp@graphite-prism-489917-p6.iam.gserviceaccount.com \
  --remove-secrets GOOGLE_CREDENTIALS_JSON
```

El Sheet master debe estar compartido (Lector) con el correo de esa Service Account, y la API de Sheets habilitada en el proyecto.

---

## Notas importantes

- **`credentials.json` nunca se sube a la imagen** (`.dockerignore` lo excluye) — va como secreto de Google Secret Manager, separado del código.
- **El estado de las sesiones OAuth vive en memoria.** Si Cloud Run reinicia el contenedor (por ejemplo tras un redeploy, o si escala a cero por inactividad y luego arranca de nuevo), vas a tener que volver a aprobar el acceso en claude.ai. Para uso personal esto es un costo aceptable; si se vuelve molesto, el siguiente paso natural es persistir tokens en una base de datos en vez de un diccionario en memoria.
- **Costo:** Cloud Run cobra por uso; para el volumen de consultas de un solo usuario probablemente quedes dentro de la capa gratuita mensual de Cloud Run.
- **El MCP local (Desktop) y el de la nube son instancias independientes** — ambos leen el mismo Master, pero no comparten sesión ni estado entre sí.
