
# App Analisador de XML NFS-e Padrão Nacional

Este app importa XMLs de NFS-e padrão nacional, tanto emitidas quanto recebidas, e demonstra:

- Valor dos serviços
- Valor contábil
- ISS destacado
- ISS retido
- PIS/COFINS
- IRRF retido
- CSLL retido
- INSS/CP retido
- Outras retenções
- IBS/CBS quando houver no XML
- Resumo por tipo: Emitida, Recebida ou Não identificada
- Resumo mensal
- Exportação para Excel e CSV

## Como rodar

1. Instale o Python.
2. Abra o terminal na pasta do projeto.
3. Rode:

```bash
pip install -r requirements.txt
streamlit run app_nfse_padrao_nacional.py
```

## No Windows

Você também pode dar duplo clique em:

```text
rodar_app.bat
```

## Como classificar emitidas e recebidas

Informe o CNPJ/CPF da sua empresa na barra lateral:

- Se o CNPJ/CPF aparecer como prestador, a nota será classificada como **Emitida**.
- Se aparecer como tomador, será classificada como **Recebida**.
- Se não bater com nenhum dos dois, ficará como **Não identificada**.

## Arquivos aceitos

- XML avulso
- ZIP contendo vários XMLs
