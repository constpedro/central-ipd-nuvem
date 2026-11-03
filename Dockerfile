# Central IPD na nuvem (Render, Docker) — versão para celular
FROM python:3.12-slim

# LibreOffice (PDF das propostas), Chromium (PDF dos relatórios PPI/SISCAN) e as
# fontes dos modelos: Arial/Verdana/Times (pacote contrib) e Carlito no lugar da Calibri.
RUN sed -i 's/Components: main/Components: main contrib/' /etc/apt/sources.list.d/debian.sources \
 && echo "ttf-mscorefonts-installer msttcorefonts/accepted-mscorefonts-eula select true" | debconf-set-selections \
 && apt-get update \
 && apt-get install -y --no-install-recommends libreoffice-writer-nogui chromium ttf-mscorefonts-installer \
    fonts-crosextra-carlito fonts-liberation fontconfig tzdata \
 && fc-cache -f && rm -rf /var/lib/apt/lists/* \
 # o motor do PPI procura "chrome"; dentro do container ele precisa de --no-sandbox
 && printf '#!/bin/sh\nexec /usr/bin/chromium --no-sandbox "$@"\n' > /usr/local/bin/chrome && chmod +x /usr/local/bin/chrome

RUN useradd -m -u 1000 app
USER app
# MALLOC_ARENA_MAX=2: com várias threads o Python guarda muita memória sem devolver;
# no plano gratuito (512 MB) isso faz a diferença.
ENV HOME=/home/app PATH=/home/app/.local/bin:$PATH PYTHONUNBUFFERED=1 CENTRAL_NUVEM=1 MALLOC_ARENA_MAX=2
# horário de Brasília (o servidor fica nos EUA): datas das propostas, fila e arquivos
ENV TZ=America/Sao_Paulo
WORKDIR /home/app/central

COPY --chown=app requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt
COPY --chown=app . .

EXPOSE 10000
# Um processo só (a fila e a numeração vivem nele), com algumas threads. O Render
# informa a porta em $PORT. Sem --max-requests: trocar o processo mataria um relatório
# rodando na fila. --access-logfile: cada acesso aparece nos registros (para achar erros).
CMD ["sh", "-c", "exec gunicorn -b 0.0.0.0:${PORT:-10000} -w 1 --threads 4 --timeout 300 --graceful-timeout 120 --access-logfile - app:app"]
