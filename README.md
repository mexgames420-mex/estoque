# Mex Games - Controle de Estoque de Contas

Sistema interno web para controlar contas PlayStation e Xbox, com cadastro, filtros, dashboard, relatórios e banco SQLite local.

## Como rodar

Requisitos:

- Python 3.9 ou superior

Passos:

```bash
python3 app.py --seed
```

Depois acesse:

```text
http://127.0.0.1:8000
```

O banco será criado automaticamente no arquivo `mex_games.sqlite3`.

## Como hospedar na nuvem

O caminho mais simples para começar é usar Render com disco persistente.

1. Crie um repositório no GitHub com estes arquivos.
2. Não envie o arquivo `mex_games.sqlite3` para o GitHub; ele é banco local e está no `.gitignore`.
3. No Render, crie um novo Blueprint/Web Service apontando para o repositório.
4. Use o arquivo `render.yaml` deste projeto.
5. Configure a variável secreta `APP_PASSWORD` com uma senha forte.
6. O usuário padrão será `admin`, configurado em `APP_USER`.
7. O banco em produção ficará em `/opt/render/project/src/storage/mex_games.sqlite3`.

Importante: o serviço precisa de disco persistente. Sem isso, o SQLite pode ser perdido em redeploys ou reinícios.

Para testar o modo cloud localmente:

```bash
HOST=0.0.0.0 APP_PASSWORD=uma-senha-forte python3 app.py
```

## Funcionalidades

- Dashboard com os números principais.
- Cadastro e edição de contas.
- Filtros por plataforma, produto/jogo, tipo de mídia e status.
- Relatórios com total de contas em utilização, reenvio 60 dias, reenvio 90 dias e reenvio que não funcionou.
- Regra automática dos 60 dias: contas com status `Conta em utilização` e último envio há 60 dias viram `Disponível para teste de reenvio 60 dias`.
- Regra automática dos 90 dias: contas com status `Conta em utilização` ou `Disponível para teste de reenvio 60 dias` e último envio há 90 dias viram `Disponível para teste de reenvio 90 dias`.
- Acesso direto pelo link, sem tela de login.
- Tela `Adicionar bloco` para colar contas vindas do Google Docs sem salvar senha nem código de segurança.

## Status usados

- Conta em utilização
- Disponível para teste de reenvio 60 dias
- Disponível para teste de reenvio 90 dias
- Não funcionou o Reenvio

Quando uma conta entra em `Não funcionou o Reenvio`, o sistema conta 30 dias e depois muda automaticamente para `Disponível para teste de reenvio 90 dias`.

## Adicionar bloco do Google Docs

Use a tela `Adicionar bloco`.

No topo da tela, informe ou selecione o `Produto/Jogo` e escolha a `Plataforma padrão`.
As plataformas padrão são apenas `Playstation` e `Xbox`.
Depois cole o bloco completo do Google Docs.

O sistema faz assim:

- Usa o e-mail encontrado no bloco.
- Linhas com `PS4` ou `PS5` viram vagas `Primária`, usam plataforma `Playstation` e adicionam `PS4` ou `PS5` no nome do jogo. Exemplo: produto informado `Split Fiction` + linha com `PS5` vira `Split Fiction PS5`.
- Linhas com `SECUNDARIA` viram vagas `Secundária`.
- A última data encontrada na linha vira a `Data do último envio` daquela vaga.
- Se uma vaga não tiver data no bloco, o sistema considera que ela foi enviada há 30 dias.
- Senha, data de nascimento, usuário e códigos não são salvos.

Há um arquivo de exemplo em `sample_block.txt`.

Exemplo da regra dos 90 dias: se importar uma conta com data `01/03/26`, ela será marcada automaticamente como `Disponível para teste de reenvio 90 dias` quando essa data já tiver completado 90 dias.
