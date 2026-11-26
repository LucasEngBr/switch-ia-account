<p align="center">
  <img src="docs/banner.svg" alt="Switch IA Account" width="100%">
</p>

<p align="center">
  <b>Troque de conta no Claude Desktop e no Codex com um clique, direto da bandeja do Windows.</b><br>
  Histórico de conversas mantido, limites de uso visíveis, um app aberto por vez.
</p>

<p align="center">
  <img alt="Windows 10/11" src="https://img.shields.io/badge/Windows-10%20%7C%2011-0078D4?logo=windows&logoColor=white">
  <img alt="Python 3.10+" src="https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white">
  <img alt="MIT" src="https://img.shields.io/badge/licen%C3%A7a-MIT-green">
  <img alt="Não oficial" src="https://img.shields.io/badge/projeto-n%C3%A3o%20oficial-lightgrey">
</p>

---

<p align="center">
  <img src="docs/menu-tooltip.png" alt="Menu com contas do Claude e do Codex" width="760">
  <br><sub>Contas fictícias. Metade esquerda: Claude Desktop. Metade direita: Codex.</sub>
</p>

## O que faz

- **Visual dark Liquid Glass** (estilo iOS 26): lente com refração nas bordas, aberração cromática, brilho especular direcional e tint escuro adaptativo, renderizado sobre o que está atrás da janela.
- **Fundo do vidro configurável** (`BACKDROP` em `src/switcher.pyw`): `"noise"` (padrão) usa um degradê preto/grafite com granulado, rápido porque as lentes são calculadas uma vez; `"screen"` refrata o que está atrás da janela.
- **Texto nítido**: letras desenhadas pelo próprio Windows (ClearType) por cima do vidro, com tamanho mínimo legível e corte com `…` quando não cabem.
- **Contas ordenadas pelo limite**: primeiro as que têm mais limite restante; as esgotadas vão para o fim, da que reseta primeiro para a última.
- **Um ícone na bandeja** (silhueta de pessoa). Clique e aparece um menu dividido em duas metades: **Claude** e **Codex**.
- **Troca com um clique.** Clique numa conta e o trocador fecha o app, troca o login e reabre na conta escolhida.
- **Histórico preservado.**
  - Codex: as conversas ficam em `~/.codex` e são as mesmas para todas as contas (igual à extensão do Codex no Cursor/VS Code).
  - Claude Desktop: a lista de sessões da aba **Code** é sincronizada entre os perfis (cópia, com o Claude fechado), então você continua a conversa em qualquer conta.
- **Medidor de limite por conta**, no estilo do app original. Passe o mouse no anel:
  - normal: `Sessão (5h): X% restantes` e `Semanal: Y% restantes`;
  - sessão esgotada: quanto falta para o reset da sessão;
  - semanal esgotado: só quanto falta para renovar o semanal.
- **Adicionar conta** em cada metade:
  - Codex: abre o login no navegador (`codex login` numa pasta temporária) e guarda o resultado.
  - Claude: cria um perfil novo e abre o Claude nele para você logar.
- **Remover conta** pelo ícone de porta, com **dupla confirmação**: o 1º clique deixa a porta vermelha, o 2º abre a confirmação final.
- **Atalho `Claude` que abre a última conta usada** (Área de Trabalho e Menu Iniciar): o ícone normal do Claude sempre abre a conta padrão; esse atalho abre a conta em que você estava. O trocador também reconhece quando o Claude foi aberto pelo ícone normal.
- **Inicia com o Windows**, mesmo com o Claude e o Codex fechados.
- **Retomada automática do Codex após interrupção por limite**: se uma tarefa do Desktop encerrar com o erro estruturado `usage_limit_exceeded`, consulta as outras contas, troca para a disponível com maior limite de sessão (desempate pelo semanal) e envia “Meu último pedido foi interrompido pelo limite de sessão. Veja onde parou e continue.” na mesma conversa pelo canal local do Desktop. Funciona com a conversa fora de foco.

### Retomada por limite (Codex Desktop)

A opção **Retomar Codex ao parar por limite**, no menu da bandeja, vem ativada. O monitor acompanha novos registros de sessão a cada três segundos e só reage a um `task_complete` com `error.codex_error_info = "usage_limit_exceeded"`, associado a um turno que começou naquela conversa.

Avisos de uso próximo do fim, medidores em zero, mensagens mencionando limites, erros de rede, cancelamentos e conclusões normais não acionam a troca. Erros anteriores à inicialização do trocador também são ignorados. Se você já retomou a conversa ou mudou de conta manualmente, o evento perde a validade.

A conta escolhida precisa ter dados atuais de sessão e semanal, ambos acima de zero. Sem uma conta elegível, a tarefa fica parada. Se outra conversa do Codex ainda estiver executando, o trocador não fecha o aplicativo para trocar a conta. Dentro de uma sequência de falhas por limite, não volta às contas que já foram tentadas.

Cada interrupção pode gerar somente uma tentativa de envio da mensagem de retomada. O identificador do turno é salvo antes da troca; um envio com resultado incerto não é repetido automaticamente. O trocador verifica o início de outro turno e avisa quando a retomada foi confirmada.

Configuração e diagnóstico ficam em `~/.codex-accounts/`:

- `autoresume.json`: habilita ou desabilita o monitor (`enabled`).
- `autoresume-state.json`: eventos tratados e contas tentadas, sem tokens ou transcrições.
- `autoresume.log`: registro das decisões, com rotação de arquivos.

O canal local do Desktop é uma interface interna e pode mudar em atualizações. Se não estiver disponível, o trocador registra a falha. Esta função não usa `codex exec resume`, não cria outra conversa e não automatiza o Claude nesta versão.

Para verificar a lógica sem trocar contas reais ou enviar mensagens:

```powershell
python -B -m unittest discover -s tests -v
```

## Instalação

Requisitos: Windows 10/11, [Python 3.10+](https://www.python.org/downloads/) no PATH, Claude Desktop e/ou Codex (app ou extensão do Cursor/VS Code).

```powershell
git clone https://github.com/LucasEngBr/switch-ia-account.git
cd switch-ia-account
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

O instalador copia o app para `%LOCALAPPDATA%\SwitchIAAccount`, instala as dependências (`pystray`, `Pillow`, `cryptography`, `numpy`), cria atalhos no Menu Iniciar e na Inicialização e abre o ícone na bandeja.

Para remover: `powershell -ExecutionPolicy Bypass -File .\uninstall.ps1` (as contas salvas ficam até você apagar as pastas abaixo).

## Como funciona

<p align="center"><img src="docs/menu.png" alt="Menu" width="560"></p>

### Codex

| Item | Detalhe |
|---|---|
| Login | O Codex guarda o login em `~/.codex/auth.json`. Cada conta tem uma cópia em `~/.codex-accounts/<account_id>/auth.json`. |
| Troca | Salva o `auth.json` atual (tokens renovados) no lugar dele, fecha o app do Codex e o `codex.exe` das extensões, coloca o `auth.json` da conta escolhida e reabre o app se ele estava aberto. No Cursor/VS Code, rode **Developer: Reload Window**. |
| Histórico | Não é tocado: `state_5.sqlite` e `thread_history_1.sqlite` em `~/.codex` servem todas as contas. |
| Limites | `GET chatgpt.com/backend-api/wham/usage` com o token da conta (o mesmo que o Codex usa). Contas inativas com token vencido são renovadas e salvas de volta na pasta delas. A conta ativa nunca é renovada pelo trocador. |

### Claude Desktop

| Item | Detalhe |
|---|---|
| Perfis | Cada conta é uma pasta de dados do Electron. Perfis já existentes na máquina são importados sozinhos; novos ficam em `~/.claude-accounts/profiles/`. |
| Troca | Fecha o Claude e reabre com `claude-desktop.exe --user-data-dir=<perfil>`. Só um aberto por vez. |
| Sessões do Code | Cada perfil mantém sua pasta `claude-code-sessions/<conta>/<org>` real. A cada troca (e ao abrir pelo atalho `Claude`), com o app fechado, o trocador copia as fichas entre os perfis: a versão mais nova vence e sessões apagadas são apagadas em todos. Não usa link/junction: o Claude recusa gravar em pasta que seja link (proteção contra *symlink plant*) e as sessões se perdiam. Transcrições, skills e plugins já são compartilhados por `~/.claude`. |
| Limites | O login do perfil é lido do `config.json` (cache criptografado pelo Electron com DPAPI + AES-GCM, só para o seu usuário do Windows) e usado em `GET api.anthropic.com/api/oauth/usage`. O trocador não renova o login do Claude: se vencer, o anel mostra `!` até você abrir aquela conta uma vez. |
| Limitação | Conversas do chat comum (claude.ai) ficam na nuvem de cada conta e não são compartilhadas. |

### Arquivos

```
%LOCALAPPDATA%\SwitchIAAccount\   o app
~/.codex-accounts/                logins do Codex (auth.json por conta) + accounts.json
~/.claude-accounts/               profiles.json + perfis novos do Claude
```

## Segurança

- `~/.codex-accounts` guarda tokens do Codex **sem criptografia extra**, igual ao próprio `~/.codex/auth.json`. Não sincronize essa pasta (OneDrive, Dropbox) nem compartilhe.
- Os tokens do Claude continuam dentro dos perfis, criptografados pelo próprio app. O trocador só os lê localmente para consultar o uso.
- Nada é enviado para terceiros: as únicas chamadas de rede vão para `chatgpt.com`, `auth.openai.com` e `api.anthropic.com`, com o token da própria conta.

## Gerar as imagens

```powershell
python tools\render_demo.py
```

Renderiza o menu com contas fictícias em `docs/` e gera `src/icon.ico`.

## Aviso

Projeto pessoal, **não oficial**, sem vínculo com a Anthropic nem com a OpenAI. Usa formatos de arquivo e endpoints internos que podem mudar a qualquer atualização dos apps. Respeite os termos de uso de cada serviço: o trocador só alterna entre contas que são suas.

## Licença

[MIT](LICENSE)
