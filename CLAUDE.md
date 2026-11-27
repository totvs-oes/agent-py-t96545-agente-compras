---
name: "iben-agente-compras"
description: "Contexto do projeto TOTVS IBEN (agentes de IA de compras no RM via LYNN). Use ao trabalhar, tirar dúvidas ou escrever specs/código do projeto IBEN."
---

# Projeto IBEN – Implantação de Agentes de IA | Agente de Compras

Base consolidada a partir das atas de discovery (11/08, 14/08, 19/08, 21/08 e 03/09/2026), das especificações funcionais (v1.0 de 24/09 e v1.1 de 16/10), das especificações técnicas dos Agentes 1 e 2, do plano de pendências e da planilha de carteiras. Situação registrada em 23/11/2026, quando o Jamil entrou no projeto, e atualizada em 24/11/2026.

## Status atual (24/11/2026)
- **Agente 1:** pronto, construído pelo Kaio. Rodou em homologação em 17/11 (exemplo: solicitação 8550 / Mov. 000001411 → cotação 2026.000401)
- **Agente 2: foco atual. É a entrega do Jamil**
- **Agente 3:** na fila, sem previsão

## Identificação
- Cliente: IBEN ENGENHARIA S.A. (código T96545). Segmento: construção e projetos
- Projeto: D00084368001. Proposta: AARHBP. Unidade TOTVS: IDeIA – O&S
- ERP: TOTVS RM hospedado no TCloud. Módulos: Obras e Projetos (TCOP), Compras, TOTVS Aprovações
- Plataforma dos agentes: LYNN (Agent Builder), com segredos no cofre nativo do LYNN (`context.secrets.URL` = {urlServidor})
- Volume: cerca de 700 solicitações por mês e ~10 min de trabalho manual por item hoje
- Princípio: o agente faz o trabalho operacional; escolher a oferta vencedora e aprovar o pedido continuam sendo ações humanas

## Pessoas
- TOTVS:
  - Denise Marcia Nasario Carneiro: líder do projeto
  - Vicente Albuquerque De Lacerda: gerente/coordenador
  - Gabriel Alves Soares: especificações funcionais (substituiu o Jeff)
  - Kaio Pablo Martins: especificações técnicas e versão v1.1 das funcionais
  - Louis Fernando Vicente: autor do documento de escopo original (OS de 25/05)
  - Bianca Rodrigues e Ademir Bonin: comercial/executivos de conta
- IBEN:
  - Wélliton Oliveira Silva: coordenador do lado do cliente, key user do RM e quem assina o aceite
  - Fernanda Piedemonte: Suprimentos
  - Marina Zonaro: Suprimentos
  - Joyce, Viviane, Amanda e Simone: insumos e orçamento
  - Perlini: ponto focal nas férias do Wélliton
- Compradores por carteira:
  - Materiais: Vagner Aguiar (aço, blocos, elétrico...), Paulo Camargo (concreto, hidráulico, cimento...), Jessica Miguel (esquadrias, portas, revestimentos...), Zeniane Silva (EPI, escritório, diversos)
  - Serviços/locações: Rafael Silva (mão de obra de estrutura/alvenaria/instalações), Suzane Andrade (terraplenagem, fundações, gesso...), Anderson Dias (locações de forma, escora, grua...)
- Status semanal: quartas às 14h

## Fluxo: 6 subprocessos em 3 agentes

### Agente 1 – Requisição e Cotação (subprocessos 1 a 3)
É o que tem a especificação técnica mais detalhada (v1.0, Kaio). O agente principal usa o modelo gpt-4o-mini; o Assistente de Cotação e o Reportador usam gemini-3.1-flash-lite.

Subagentes:
1. **Emissor de Requisições** (roda por CRON próprio)
   - Consulta TOTVS_700 (PRODUTOS_POR_TAREFA)
   - Gera o pedido com o processo `PrjProcGeraPedidoMaterialData` via JobScheduler, considerando 15 a 30 dias de antecedência e o mês da necessidade
2. **Movimentador de Pedidos**
   - Consulta TOTVS_700.1 (TCOP_REQ_PENDENTES)
   - Envia com o processo `PrjEnviaUnicoMovimentoProc`, em lotes de até 1.000 por coligada/projeto
3. **Classificador de Comprador**
   - Consulta TOTVS_701 (REQ_COMPRAS_PENDENTES: movimento 1.1.02 sem comprador)
   - `GET` e depois `PUT /api/mov/v1/Movements/{CODCOLIGADA}_{IDMOV}` gravando o comprador sugerido
4. **Assistente de Cotação**
   - Consulta TOTVS_702 (REQUISICOES_PENDENTES_ITENS) para os grupos e TOTVS_703 (OBTEM_MAPA_DE_COTACAO, `?parameters={LISTA_IDPRD}`) para os fornecedores
   - Usa um LLM para gerar a descrição (até 200 caracteres)
   - Cria a cotação com o processo `CmpAssistenteCotacaoProc`
   - Condição de pagamento: 099 para produto, 008 para serviço
5. **Comunicador de Fornecedores**
   - Consulta TOTVS_704 (COTACOES_PENDENTES_DE_ENVIO) e envia só aos fornecedores marcados como APTO
   - Chamada SOAP em `{url}/wsProcess/IwsProcess`, operação `ExecuteWithXmlParamsAsync`, processo `CmpCotacaoComunicarFornecedoresProc`
   - Registra o JobID e o ExecID retornados
6. **Reportador de Execução**
   - Espera 120s e consulta TOTVS_705 (`DATA_INICIO`/`DATA_FIM` no fuso America/Sao_Paulo)
   - Manda um relatório único por e-mail (ferramenta `enviar_email`)

Consultas SQL: `{url}/api/framework/v1/consultaSQLServer/RealizaConsulta/<COD>/0/M`. Jobs: `POST {url}/api/framework/v1/jobScheduler`.

**Importante:** o job ser aceito NÃO confirma que o processo terminou nem que o e-mail foi entregue. É preciso conferir no monitor de jobs.

### Agente 2 – Equalização (subprocessos 4 e 5) — FOCO ATUAL
Especificação técnica genérica, com lacunas. Modelo: Gemini 3.1 Flash Lite em todos os subagentes.

**Decisão de 24/11/2026 (Denise e Gabriel): o cliente NÃO vai adotar planilha padrão para os fornecedores por enquanto.**
- O agente precisa ler o que chegar por e-mail: PDF (inclusive escaneado), corpo do e-mail, Excel/planilha em formato livre
- A extração deve ser por LLM, com modelo que leia PDF/imagem, sem depender de layout fixo
- A tradução de produto (fornecedor → código RM) passa a ser obrigatória e central

**Estratégia de tradução de produto (proposta):**
1. O casamento é feito só contra os itens DAQUELA cotação (poucos itens), não contra o catálogo inteiro
2. Ordem de tentativa:
   - o fornecedor repetiu o código RM ou a descrição exata → casamento direto
   - o código do fornecedor já existe na tabela de tradução (`TPRDCFOCOLAB`, campo CODPRODFORN) → direto
   - LLM/fuzzy comparando descrição + unidade + quantidade com os itens da cotação, com nota de confiança
3. Confiança baixa ou ambiguidade → não grava e encaminha ao comprador (exceção "produto sem correspondência" da funcional)
4. Depois de confirmar o casamento, grava `ProviderProductCode` no item, para a próxima vez ser direta
5. Cuidar da unidade de medida (caixa × unidade). Há rotas de conversão: `budgetNegotiation/unitValue/...` e `totalValue/...`

Subagentes:
1. **Leitor/Extrator**
   - Lê a caixa centralizada compras@ por IMAP
   - Identifica o número da cotação no assunto, no corpo ou no nome do anexo
   - Extrai preço unitário, prazo, validade, frete, condição de pagamento e contato
2. **Registrador**
   - Faz a tradução de produto pela tabela `TPRDCFOCOLAB` (`:CODPRODFORN`)
   - `PUT /api/cmp/v1/budgetNegotiation/budget/{id}` e `.../budgetItem/{id}` (ou `negotiatedItem/{id}`), deixando o item como "Cotado"
   - Grava o resumo nas observações internas e anexa as propostas
3. **Equalizador**
   - Calcula o quadro comparativo (é obrigatório antes de finalizar)
   - Equaliza item a item: urgência alta prioriza prazo
   - Compara com a última compra (TOTVS_HIST_COMPRAS)
4. **Recomendador**
   - Sugere o vencedor (`/api/supply/v1/quotations/{CODCOTACAO}/selectWinner` — **essa rota NÃO existe** no Swagger da base, verificado em 24/11)
   - Avisa o comprador quando o preço foge muito do esperado

O comprador valida a sugestão.

### ✅ Caminho que FUNCIONA para gravar orçamento: SOAP wsDataServer (validado em 24/11/2026)
Testado na cotação 2026.000401, fornecedor 0018740: preço 9,80 e prazo 7 gravados, DESVIO calculado pelo RM = 0,6411. Script de referência: `rm_cotacao.py` (em `C:\temp\vscode\iben-leitor-pdf`).
- Endpoint: `{url}/wsDataServer/IwsDataServer`, autenticação Basic com o Ag.ia_SUPRI. SOAPAction `http://www.totvs.com/IwsDataServer/<ReadRecord|SaveRecord|GetSchema|ReadView>`
- Contexto: `CODCOLIGADA=42;CODFILIAL=1;CODSISTEMA=T;CODUSUARIO=Ag.ia_SUPRI`
- **DataServer da cotação: `CmpCotacaoData`**. Chave do ReadRecord: **`CODCOTACAO;CODCOLIGADA`**, nessa ordem (ex.: `2026.000401;42`). Na ordem inversa dá "Input string was not in a correct format"
- DataServer do movimento: `MovMovimentoTBCData`, chave `CODCOLIGADA;IDMOV` (ex.: `42;8550`)
- Fluxo de gravação: ReadRecord → remover os blocos `_x0024_IMAGES` → alterar só as linhas do fornecedor → SaveRecord com o XML em CDATA → reler para conferir
- Sucesso: o SaveRecord devolve a chave (`2026.000401;42`). Erro de regra: devolve a mensagem + stack trace. A gravação é transacional (em erro, nada é gravado)
- **⚠️ DECIMAL:** o ReadRecord devolve com ponto (`9.8000`), mas o SaveRecord deste servidor lê em pt-BR (ponto = milhar). Mandar `9.8000` grava **98000**; `500.0000` virou 5.000.000 em todas as linhas reenviadas. Converter para vírgula antes de gravar, **mas nunca em campos de código** (`CODCOTACAO` "2026.000401" parece decimal; convertido, vira "2026,000401")
- **Regra do RM:** se o prazo de entrega for maior que 0, `DATAENTREGAORC` (TCORCAMENTO) e `DATAENTREGA` (itens) precisam ser = `DATCOTACAO` + prazo. Validade: `DATAVALIDADEORC` = `DATCOTACAO` + `VALPRAZOVALIDADE`. Quando o fornecedor informar uma data, converter em prazo (data − data da cotação)
- Boa prática: sempre simular e listar a diferença (campo: antes → depois) antes do SaveRecord, porque o registro inteiro é reenviado

**Tabelas do CmpCotacaoData (do GetSchema):**
- `TCCOTACAO` (cabeçalho): DATCOTACAO, DATLIMRESPTA, CODCOMPRADOR, STSCOTACAO, CODCPG, TXTOBSERVACAO, DESCRICAO, TIPOJULGAMENTO
- `TCCOTACAOITMMOV` (itens vindos da 1.1.02): IDMOV, NSEQITMMOV, NUMEROMOV, IDPRD, CODIGOPRD, NOMEFANTASIA, QUANTIDADE, PRECOUNITARIO (referência/orçado), CODUND
- `TCORCAMENTO` (1 por fornecedor; chave cotação+coligada+CODCFO+CODCOLCFO): VALPRAZOENTREGA, DATAENTREGAORC, VALPRAZOVALIDADE, DATAVALIDADEORC, CODCPG, FRETECIFOUFOB (9 = sem frete), VALORFRETE, VALFRETE (%), DESPESA, DSCCONTATO, TELCONTATO, EMAIL, CODMOVIMENTOCFO (nº do orçamento do fornecedor), TXTOBSERVACAO (resumo), STATUSRESPOSTA, DECLINADO, MOTIVODECLINIO
- `TCITMORCAMENTO` (fornecedor × item; chave inclui IDMOV, NSEQITMMOV, CODCOLMOV): **VALCOTACAO (preço unitário cotado)**, VALNEGOCIADO, QUANTIDADEORC, CODUNDORC, PRAZOENTREGA, DATAENTREGA, OBSERVACAO, STSITEM (2 = pendente, 0 = cotado), **CFOVENCEDOR (1 = vencedor do item, alternativa ao selectWinner)**, CFOITEMVENCEDORCOT, DESVIO (= PRECOUNITARIO − VALCOTACAO, calculado pelo RM), PERCECONOMIA, VALEQUALIZADOCOT
- `TCITMORCAMENTOAGRUPADO`: espelho de TCITMORCAMENTO por produto. Atualizar os dois juntos
- -1 em prazo = não informado

**API REST do Portal do Fornecedor (`budgetNegotiation`) — NÃO funciona com o Ag.ia_SUPRI:** todas as rotas, inclusive `checkPermission`, dão 401. Documentação abaixo só como referência.

**API real (Swagger "APIs Back-Office", verificado em 24/11):** as rotas `budgetNegotiation` são a API do Portal do Fornecedor (operações "ByCFO").
- Leitura:
  - `GET /api/cmp/v1/budgetNegotiation/{id}/{situacao}` (cotações por fornecedor)
  - `GET .../negotiations/{id}` e `.../negotiations/{id}/items`
  - `GET .../budgetItemList/{id}`
  - NÃO existe GET em `/budget/{id}` nem em `/budgetItem/{id}`
- Escrita:
  - `PUT .../budget/{id}` (CmpOrcamentoCotacaoDTO)
  - `PUT .../budgetItem/{id}` e `PUT .../multipleBudgetItens` (CmpItemOrcamentoCotacaoDTO)
  - `PUT .../negotiatedItem/{id}`
  - `POST .../attach` (CmpAnexoOrcamentoDTO: FileName, File em base64)
- Cabeçalho (CmpOrcamentoCotacaoDTO): QuotationCode, VendorCode, PaymentTermCode, DeliveryDate, ExpirationDate, FreightType, FreightValue, DiscountValue, ExpenseValue, StatusResponse, Declined, ReasonDeclined
- Item (CmpItemOrcamentoCotacaoDTO): QuotationValue, DeliveryTime, ProviderProductCode, BudgetedQuantity, Unavailable, PurchaseRequestId (vínculo com o 1.1.02), PriceDeviation e Saving (o RM calcula)
- O formato do `{id}` não foi descoberto (todas as tentativas deram 401)
- Não há rota para calcular o quadro comparativo. Pode ser processo via wsProcess

### Agente 3 – Ordem de Compra (subprocesso 6)
Só tem especificação funcional (v1.1). **Não há especificação técnica**, e a reunião de validação ficou pendente.
- Gera o Mov. 110 herdando da cotação: fornecedor, itens, quantidades, preços, prazo, frete e condição de pagamento
- Mantém o vínculo com o Mov. 102, o projeto e a tarefa da EAP
- Anexa as propostas e os relatórios do mapa (acaba com o PDF montado à mão)
- Valida o valor contra os parâmetros autorizados
- Envia ao TOTVS Aprovações (status 110 e, depois de aprovado, 15)
- Monitora o status e avisa em caso de reprovação ou de atraso
- Não reenvia automaticamente um pedido reprovado

## Ambiente e acesso (verificado em 24/11/2026)
- **Homologação:** `https://ibenengenharia173927.rm.cloudtotvs.com.br:1558`, banco CORPORERM-DEV. REST, Swagger (`/api/swagger/`) e TBC SOAP (`/wspageindex/`, `/wsProcess/IwsProcess`) ficam no mesmo host
- **⚠️ PRODUÇÃO:** `ibenengenharia173926.rm.cloudtotvs.com.br:8051`, banco CORPORERM-PRD. Era o link "TBC Soap" passado pelo Gabriel. NÃO USAR
- SmartClient instalado em `C:\TOTVS\RM.NET\RM.exe`. Ambiente marcado como "TESTE", coligada 42 – IZZI RESIDENCE, filial 1 (24 de Maio)
- Usuário do agente: `Ag.ia_SUPRI` (senha guardada fora daqui). Tem permissão só para o 1.1.02; para o Mov. 110 (OC) a lista vem vazia
- **Fornecedor de teste:** 0018740 ("68.729.015 KAIO PABLO MARTINS"). Usar só ele em testes de envio de e-mail, para não atingir fornecedor real
- Telas do RM: Compras → Solicitação de Compra (1.1.02), Cotação (filtro "Período"; abas Orçamentos e Itens de Movimento, onde "Mov. Origem" = nº da 1.1.02), Ordem de Compra. Comprador em Identificação → Tabelas
- **JobScheduler:**
  - `POST /api/framework/v1/jobScheduler` com o corpo `{"ProcessId": "<Classe>", "ExecutionParameter": {...}}`
  - `GET /api/framework/v1/jobScheduler?order=-jobScheduleId` lista os jobs, mais recentes primeiro
  - `GET /api/framework/v1/jobScheduler/{id}` traz o ExecutionParameter real de um job
  - `paramsProc/{processInfo}` volta vazio com só o nome da classe
  - O ExecutionParameter tem PrimaryKeyList, PrimaryKeyNames, PrimaryKeyTableName e Context._params (com $CODCOLIGADA, $CODFILIAL, $CODSISTEMA: M = Obras, T = Compras, F = Financeiro)
  - Limite de 1.000 chaves por processo
- Exemplo real do Comunicador (job 1176417, 23/11):
  - CodCotacao 2026.000401, Fornecedores [{CodCfo, Contatos}], DataLimiteResposta
  - AssuntoEmail "Pedido de Orçamento nº 2026.000401"
  - IdRelatorio 884 anexado (RelatorioAnexoEmail true), EmailSomenteContato true
  - CopiaComprador false, o que diverge da spec, que pede cópia obrigatória
- Coleção do Postman "IBEN · Agente de Compras" criada com as variáveis url_rest, url_soap, user, pass
- **REST de movimentos:**
  - O RM usa `|` nas chaves (`internalId "42|8550"`)
  - `GET /api/mov/v1/Movements/{id}` direto dá erro FE006 ("Index was outside the bounds")
  - O que funciona: `GET /api/mov/v1/Movements?companyId=42&movementId=8550&PageSize=1`
  - Campos: `salesman1Code` / `CODVEN1` = comprador; `purchaseStatus` / `STSCOMPRAS` = status de compras (C = em cotação); `classificationTable3Code` / `CODTB3FAT` = grupo do produto; `unitPrice` = preço orçado; `taskCode` / `CODTRF` = tarefa da EAP
- **Homologação tem e-mails mascarados:** os fornecedores reais da cotação 401 (ESTEVES 0002158, BLUKIT 0002594, TIGRE 0012410) estão com e-mail `iben.sistemas@iben.eng.br`. Só o fornecedor de teste 0018740 tem o e-mail do Kaio
- Estado da cotação 2026.000401 após os testes de 24/11:
  - ESTEVES com 500,00, gravado em 19/11 (provável teste do Kaio; parece total digitado como unitário)
  - 0018740 com 9,80, prazo 7, validade 15
  - BLUKIT e TIGRE pendentes

## Regras de negócio principais
- **Comprador:** vem só do Grupo de Produtos do RM (sem planilha externa). Férias e trocas se resolvem atualizando o cadastro do grupo
- **Escopo do agente:** atua em todas as requisições dos grupos autorizados, mesmo as que não criou
- **Fornecedores convidados:**
  - os dos 3 últimos mapas de cotação do produto, mais 2 sorteados do grupo
  - só entram os vinculados ao grupo e com e-mail comercial válido
- **Pagamento:** 28 dias para material; medição mensal + 15 dias para serviço
- **Prazo de resposta:** a decisão de 03/09 foi 10 dias ou 3 propostas, o que vier primeiro. A planilha de carteiras tem prazos por grupo de 5 a 20 dias
- **E-mail de cotação:**
  - texto padrão definido no RM
  - número da cotação obrigatório, e o fornecedor deve repeti-lo na resposta
  - checklist (frete, pagamento, prazo, validade)
  - cópia para o comprador; respostas vão para a caixa compras@
- **Automação:** só para insumos padronizados; os variáveis seguem modelo misto. Aprovação automática apenas dentro da faixa de preço; fora dela, análise humana
- **Fora do escopo:**
  - WhatsApp e negociação por chat com o fornecedor
  - Portal de Fornecedores
  - limpeza de cadastros (responsabilidade da IBEN)
  - Contas a Pagar e Contas a Receber
- **Erros:** registrar em log (data/hora, sistema, chave, mensagem), tentar de novo (quantas vezes e com que intervalo ainda não foi definido), notificar e seguir para o próximo item

## Inconsistências já identificadas nos documentos
1. Técnica do Agente 1: as descrições do Comunicador de Fornecedores e do Reportador de Execução estão trocadas
2. Técnica do Agente 2:
   - `getTraducaoProduto` usa TOTVS_704, código que no Agente 1 já é COTACOES_PENDENTES_DE_ENVIO
   - `calcularQuadroComparativo` não tem endpoint
   - `proporVencedor` usa `/api/supply/v1` (confirmar se existe no RM)
   - o objetivo ainda diz "Agente de Requisição e Cotação"
3. A funcional do Agente 2 pede itens que a técnica não cobre:
   - leitura de PDF (a técnica fala só de planilha)
   - resposta automática ao fornecedor quando a planilha vem incompleta
   - aviso aos fornecedores que não responderam até a data limite
4. Prazo de resposta: a regra de "10 dias ou 3 propostas" foi decidida para serviços e conflita com os prazos por grupo da planilha
5. Não está definido de onde vem a "urgência alta/baixa" usada na equalização
6. A faixa de preço que permite decisão automática não foi definida (G-9 / G-2)
7. A funcional fala em operação "online/contínua", mas as premissas e a técnica usam agendamento (CRON)

## Pendências do cliente (bloqueadoras)
- Limpeza do cadastro de fornecedores (grupos e e-mails comerciais): em standby
- Mapeamento dos insumos padronizados e do tipo de compra (volume único x ressuprimento): Wélliton com Joyce e Viviane
- Checklist de cotação por tipo de material (existe para concreto)
- Texto padrão do e-mail parametrizado no RM
- Planilha padrão de orçamento (G-7): **descartada por enquanto** (decisão de 24/11). O agente lê formato livre
- Avaliação de anexar PDFs automaticamente ao mapa de cotação
- Ambiente:
  - usuário de homologação com perfil de comprador, sem admin
  - TBC ativo
  - licença de integração
  - Go Global/RDP, se necessário
  - caixa compras@ com IMAP/SMTP

## Plano de atuação do Jamil (proposto em 23/11/2026)
- **Etapa 0 – Ambientação:**
  - pedir os acessos: LYNN, RM homologação, TBC, caixa compras@
  - alinhar com o Kaio e o Gabriel o que já está construído
  - estudar as consultas TOTVS_700 a 705
- **Etapa 1 – Agente 1:** rodar de ponta a ponta em homologação e testar os critérios de aceitação
- **Etapa 2 – Agente 2 (foco atual):**
  - Fase 1: ler a cotação 401 pela API (fornecedor 0018740); levar as decisões pendentes ao time (faixa de desvio, origem da urgência, como sinalizar o vencedor, quem responde a proposta incompleta); corrigir a especificação técnica
  - Fase 2: protótipo em Python fora do LYNN, em ciclo fechado com o fornecedor de teste: IMAP → identificar a cotação → extrair do PDF/e-mail com LLM → traduzir produto → gravar via SOAP SaveRecord (CmpCotacaoData) → anexar a proposta (caminho do anexo via SOAP ainda a descobrir)
    - ✅ 24/11: gravação do orçamento no RM validada (`rm_cotacao.py`)
    - Próximo: extrator de PDF usando as propostas reais em `C:\iben\agente2\PROCESSOS`
  - Fase 3: equalização por item, histórico de compras, regra de urgência, sugestão e notificação
  - Fase 4: portar para o LYNN no padrão do Agente 1 e homologar com a Fernanda e a Marina
- **Etapa 3 – Agente 3:** fazer a reunião de validação, escrever a técnica e construir
- **Etapa 4:** homologação com a IBEN, aceite (Wélliton), entrada em produção e acompanhamento pós-implantação
- **Em paralelo:** acompanhar as pendências do cliente

## Glossário
- **Coligada:** empresa do grupo no RM
- **EAP:** Estrutura Analítica de Projeto (tarefas da obra)
- **TCOP:** módulo Obras e Projetos
- **Mov. 1.1.02 / 102:** requisição de compra
- **Mov. 110:** ordem de compra
- **Mapa de cotação / quadro comparativo:** comparação das propostas dentro do RM
- **Tradução de produto:** de-para entre o código do fornecedor e o código interno
- **TBC:** TOTVS Business Connector (web services do RM)
- **TAA:** TOTVS Aprovações