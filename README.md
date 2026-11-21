# Cofry Processamento

API FastAPI para transformar vídeos longos em cortes prontos para Reels, TikTok e Shorts. O gancho é escrito a partir do que o trecho realmente entrega e aparece nos primeiros três segundos. A seleção prioriza frases completas, autonomia do trecho e relevância para o foco definido.

## Rodar localmente

Requer Python 3.12 no Linux ou Docker. Para a execução sem Docker, instale FFmpeg/ffprobe com libass, as fontes DejaVu e, para YouTube, Node.js compatível com o yt-dlp (20 ou superior). Preencha as variáveis em uma cópia de `.env.example` chamada `.env`. Gere chaves diferentes com `openssl rand -hex 32`.

```sh
docker build -t cofry-processamento .
docker volume create cofry-data
docker run --rm -p 8000:8000 --env-file .env -v cofry-data:/data cofry-processamento
curl http://localhost:8000/health
```

Sem Docker, mantendo a API local em uma única instância:

```sh
python3.12 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
set -a
. ./.env
set +a
export HF_HOME="$(pwd)/data/huggingface"
uvicorn cofry.main:app --workers 1 --port "${PORT:-8000}"
```

O modelo Whisper é baixado no primeiro job e armazenado no cache. Em produção, comece com 2 vCPU, 4 GB de RAM e 10 GB de disco persistente. A primeira execução pode demorar mais por causa do download do modelo.

## API

Envie `X-API-Key: <PROCESSING_API_KEY>` nas chamadas protegidas. Faça essas chamadas no servidor do Lovable. Exponha o endereço como `PROCESSING_API_URL` e guarde `PROCESSING_API_KEY` e `WEBHOOK_SECRET` nos segredos do servidor.

| Rota | Resposta |
| --- | --- |
| `GET /health` | `200` com `ok` e `url_download_habilitado`. |
| `POST /jobs` | `202` com `job_id` e `status`. Repetir o mesmo pedido retorna o estado existente. Reutilizar o ID com dados diferentes retorna `409`. |
| `GET /jobs/{id}` | `status`, `etapa`, `progresso`, `erro`, `cortes`. Estados possíveis `queued`, `running`, `completed`, `failed`, `cancelled`. |
| `POST /jobs/{id}/cortes/{corte_id}/rerender` | `202` para recolocar somente o corte na fila. O resultado anterior fica disponível durante o trabalho e em caso de falha. |
| `DELETE /jobs/{id}` | `202` para cancelar e agendar a exclusão dos arquivos, inclusive os já publicados. |

Exemplo de envio. Substitua os dois endereços por URLs HTTPS que o serviço consiga acessar.

```sh
curl -X POST http://localhost:8000/jobs \
  -H "X-API-Key: $PROCESSING_API_KEY" -H 'Content-Type: application/json' \
  --data '{
    "job_id":"aula-001",
    "video_url":"https://origem.example/aula.mp4",
    "callback_url":"https://app.example/api/processamento/webhook",
    "options":{
      "quantidade":3,"duracao_min":15,"duracao_pref":30,"duracao_max":50,
      "formato":"9:16","enquadramento":"preencher","seguir_rosto":true,
      "estilo_legenda":"karaoke","posicao_legenda":"inferior",
      "gancho":true,"manchete":false,"idioma":"pt",
      "foco":"Descobertas que mudam como ensinar",
      "palavras_chave":["aprendizado"],"evitar":["avisos administrativos"],
      "trecho_inicio":0,"trecho_fim":null
    }
  }'
curl -H "X-API-Key: $PROCESSING_API_KEY" http://localhost:8000/jobs/aula-001
```

`formato` aceita `9:16`, `1:1` e `16:9`. `enquadramento` aceita `preencher`, `inteiro` e `desfocado`. O rastreamento atua em `preencher`, onde há margem para mover o crop. Quando aparecem vários rostos, a prioridade usa movimento na região da boca como aproximação de quem fala. O crop permanece centralizado quando a detecção não encontra rostos.

Os estilos são `branco`, `amarelo`, `neon_verde`, `caixa`, `minimalista`, `editorial`, `karaoke`, `contorno`, `italico_caixa` e `laranja`. As posições são `superior`, `central` e `inferior`. O idioma pode ser `pt`, `pt-BR`, outro código aceito pelo Whisper ou `auto`. O texto falado fica no idioma do áudio. Título, gancho e textos para postagem são gerados em português do Brasil.

Cada corte tem `id`, `inicio`, `fim`, `duracao`, `titulo`, `gancho`, `nota`, `motivo`, `url_mp4`, `url_capa` e `legendas` com `instagram`, `tiktok` e `youtube`. Os três campos de tempo se referem ao trecho original em segundos. O MP4 pode ficar mais curto após a remoção de pausas. A API pode entregar menos cortes do que o número solicitado quando faltam trechos compatíveis e sem sobreposição.

O rerender aceita `inicio`, `fim` e, opcionalmente, `legenda`, `gancho` e `manchete`. Os tempos das legendas editadas são absolutos no vídeo original. Omitir `legenda` mantém as legendas automáticas. Uma lista vazia desativa as legendas faladas. Gancho e manchete aceitam booleano ou texto personalizado. Mantenha o link de origem acessível enquanto o usuário puder editar o corte. O serviço baixa o original novamente e reaproveita a transcrição do SQLite.

```json
{
  "inicio": 30.5,
  "fim": 48.2,
  "legenda": {
    "estilo": "amarelo",
    "blocos": [{"inicio": 31, "fim": 34.5, "texto": "Comece por esta pergunta."}]
  },
  "gancho": "Essa pergunta muda a aula",
  "manchete": true
}
```

### Webhook

O corpo é o mesmo JSON do GET no momento em que o trabalho termina ou falha. Use `X-Job-Id` para identificar o job e `X-Signature` para comparar, em tempo constante, com o HMAC-SHA256 hexadecimal dos bytes exatos do corpo usando `WEBHOOK_SECRET`. Faça a validação antes de parsear ou reserializar o JSON. O serviço faz até três tentativas, com esperas de 2 e 4 segundos. O receptor deve deduplicar entregas, pois uma interrupção após o POST pode repetir uma entrega que já chegou. Responda com qualquer código `2xx` para confirmar.

### Origem e arquivos

Use links HTTPS diretos, links de compartilhamento do Google Drive acessíveis por download ou links do Dropbox. Downloads e callbacks só se conectam a endereços públicos na porta HTTPS padrão. O processamento valida vídeo, áudio, duração de pelo menos dois minutos, tamanho máximo e resolução de até 4096×2160 pixels equivalentes. O YouTube pode ser ativado para vídeos próprios ou autorizados e depende da disponibilidade do yt-dlp na rede de saída.

O bucket precisa servir leituras públicas pelo endereço `STORAGE_PUBLIC_BASE_URL`. No R2, use um domínio público associado ao bucket e configure uma regra de expiração de 31 dias para o prefixo de saída. No Supabase, use um bucket público e `https://<project-ref>.supabase.co/storage/v1/object/public/<bucket>` como base. O worker registra e remove os objetos ao vencerem 31 dias, inclusive no Supabase. A remoção pode atrasar se o serviço estiver indisponível, ocupado ou o provedor estiver processando a expiração. As URLs são públicas e a validade é controlada pela remoção dos objetos. Preserve o volume para manter a fila, os registros de expiração e as transcrições necessárias ao rerender.

## Variáveis de ambiente

| Variável | Valor |
| --- | --- |
| `PROCESSING_API_KEY` | Obrigatória. Chave das rotas protegidas. |
| `WEBHOOK_SECRET` | Obrigatória. Chave HMAC dos callbacks. |
| `WHISPER_MODEL` | `small`. Pode ser outro modelo faster-whisper ou um caminho local. |
| `LLM_PROVIDER` | `openai`, `anthropic` ou `openrouter`. Padrão `openai`. |
| `LLM_API_KEY` | Obrigatória para seleção. Chave do provedor da IA. |
| `LLM_MODEL` | Opcional. Padrões `gpt-4.1-mini`, `claude-sonnet-4-6`, `openai/gpt-4.1-mini`, respectivamente. Para OpenRouter, use um modelo com suporte a resposta JSON. |
| `STORAGE_ENDPOINT_URL` | Obrigatória. Endpoint HTTPS S3. Para Supabase, use o endpoint S3 informado no painel. |
| `STORAGE_REGION` | `auto` para R2. Para Supabase, use a região informada no painel. |
| `STORAGE_BUCKET` | Obrigatória. Bucket de destino. |
| `STORAGE_ACCESS_KEY_ID` | Obrigatória. ID da credencial S3. |
| `STORAGE_SECRET_ACCESS_KEY` | Obrigatória. Segredo da credencial S3. |
| `STORAGE_PUBLIC_BASE_URL` | Obrigatória. Base HTTPS que já aponta para o bucket público. |
| `STORAGE_PREFIX` | `cofry`. Prefixo das chaves de saída. |
| `ENABLE_URL_DOWNLOAD` | `false`. Use `true` para habilitar o YouTube. |
| `MAX_VIDEO_MB` | `500`, medido em MiB. |
| `MAX_VIDEO_SECONDS` | `3600`. Limite superior para a duração. |
| `PROCESSING_THREADS` | `2`. Threads de CPU destinadas às ferramentas de processamento. |
| `DATA_DIR` | `./data` fora do container e `/data` no Docker. Precisa ser gravável. |
| `HF_HOME` | Cache Hugging Face. No Docker, `/data/huggingface`. |
| `PORT` | `8000` no Docker por padrão. A plataforma pode injetar o valor. |

## Publicar

**Railway.** Importe o repositório como um serviço Dockerfile. Adicione um Volume em `/data`, configure as variáveis, use `/health` como Healthcheck Path e gere o domínio HTTPS. Mantenha uma réplica. O entrypoint prepara o volume e executa a API com o UID 10001.

**Render.** Crie um Web Service com Runtime Docker a partir do repositório. Adicione um Persistent Disk em `/data`, configure as variáveis, use `/health` como Health Check Path e mantenha uma instância. Escolha um plano com disco persistente e memória para o Whisper. O entrypoint prepara o volume e executa a API com o UID 10001.

O volume contém o SQLite em modo WAL, o cache do modelo e os arquivos temporários do job ativo. A fila retoma jobs interrompidos a partir do início. Os temporários são apagados ao encerrar cada trabalho ou na inicialização após uma interrupção. O processamento é serializado por um worker e um lock no volume.

## Testes

A suíte gera seu próprio MP4 de 126 segundos com voz sintetizada pelo filtro `flite` do FFmpeg e testa pipeline, codecs, duração, rerender, idempotência, recuperação e webhook. Os serviços externos são simulados. Opcionalmente, rode com o Whisper real na amostra de 5min05s. O teste verifica os tempos transcritos após o primeiro bloco de 180 segundos e usa a transcrição real para gerar os cortes.

```sh
pip install -r requirements-dev.txt
python -m pytest -q
TEST_VIDEO_SECONDS=305 TEST_WHISPER_MODEL=tiny python -m pytest -q
```

## Estúdio Cofry no navegador

Acesse `http://localhost:8000/studio` depois de iniciar o serviço e entre com a chave
`PROCESSING_API_KEY`. A interface está em português do Brasil e pode ser adicionada
à tela inicial pelo navegador em localhost ou em um domínio HTTPS. No Railway ou
Render, abra `https://<seu-domínio>/studio`. Configure domínio e HTTPS pelo painel
do provedor. O login é de proprietário único e cria uma sessão de 24 horas. Use a variável opcional `STUDIO_ACCESS_KEY` para separar a chave do estúdio da chave da API.

Você pode enviar o arquivo diretamente, colar um link HTTPS e ajustar formato,
quantidade, duração, foco, gancho, manchete, rosto e legendas. A mesma tela mostra
a fila e os cortes, disponibiliza os MP4 e capas e permite editar um único corte.
Para iniciar sem Docker, após preencher `.env` e instalar as dependências, execute
`sh run-studio.sh`. Em produção, use o Dockerfile e monte o volume em `/data`.

No estúdio, `LLM_API_KEY` habilita seleção e ganchos gerados pelo provedor. Se
estiver ausente, o Whisper continua transcrevendo e a seleção usa a transcrição,
o foco e as durações. Nesse modo, o gancho é extraído da própria fala. A API
pública `/jobs` continua exigindo configuração da IA e de S3.

Por padrão, a interface guarda os cortes no volume e protege os downloads com
a sessão do estúdio. Configure `STUDIO_USE_S3=true` e as variáveis `STORAGE_*`
para publicar os cortes do estúdio no bucket. Os originais enviados pelo estúdio
ficam no volume para permitir rerender e são apagados ao excluir o projeto ou
após 31 dias. Links de origem precisam continuar acessíveis para rerender.
Tempo de upload e recursos disponíveis dependem do plano de hospedagem.

A instalação da PWA adiciona um atalho com interface independente. Gerar cortes
e acessar projetos requer conexão com o servidor que executa o FFmpeg e o Whisper.

### Blueprint do Render

O arquivo `render.yaml` configura uma instância de 2 CPU e 4 GB com disco
persistente de 10 GB. Esses recursos são pagos no Render. O Blueprint gera
chaves separadas para o estúdio, API e webhook. Forneça o provedor de IA e os
dados do S3 pelos campos de segredos do Render e copie a chave do estúdio
diretamente do painel. O endereço HTTPS atribuído pelo Render termina em
`/studio` para a interface e em `/health` para a verificação do serviço.

Em outros provedores com HTTPS terminado em proxy, use
`STUDIO_COOKIE_SECURE=true` para exigir HTTPS no cookie de sessão. O padrão
permite testar o estúdio em HTTP local.
