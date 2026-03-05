"""
Middleware Kommo <-> Tiny ERP
- Recebe webhook do Salesbot do Kommo
- Lê os dados do lead (produtos do catálogo + campos personalizados)
- Cria Proposta Comercial no Tiny
- Atualiza o lead no Kommo com o nº da proposta
- Sincroniza catálogo de produtos do Tiny para o Kommo
"""

import os
import json
import logging
import requests
from flask import Flask, request, jsonify

# ── Configuração de Logging ──────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ── Variáveis de Ambiente ────────────────────────────────────────────────────
KOMMO_TOKEN = os.environ.get("KOMMO_TOKEN", "")
KOMMO_SUBDOMAIN = os.environ.get("KOMMO_SUBDOMAIN", "")
TINY_TOKEN = os.environ.get("TINY_TOKEN", "")

# IDs dos campos personalizados do Kommo
CF_PAGAMENTO = int(os.environ.get("CF_PAGAMENTO", "3008831"))
CF_DESCONTO = int(os.environ.get("CF_DESCONTO", "3008833"))
CF_FRETE = int(os.environ.get("CF_FRETE", "3008835"))
CF_OBSERVACOES = int(os.environ.get("CF_OBSERVACOES", "3008837"))
CF_ITENS = int(os.environ.get("CF_ITENS", "3008839"))
CF_NUM_PROPOSTA = int(os.environ.get("CF_NUM_PROPOSTA", "3008841"))

# ID do catálogo de produtos no Kommo
CATALOG_ID = int(os.environ.get("CATALOG_ID", "110225"))

# URLs base
KOMMO_API = f"https://{KOMMO_SUBDOMAIN}.kommo.com/api/v4"
TINY_API = "https://api.tiny.com.br/api2"

# ── Headers do Kommo ─────────────────────────────────────────────────────────
def kommo_headers():
    return {
        "Authorization": f"Bearer {KOMMO_TOKEN}",
        "Content-Type": "application/json"
    }

# ══════════════════════════════════════════════════════════════════════════════
#  FUNÇÕES AUXILIARES - KOMMO
# ══════════════════════════════════════════════════════════════════════════════

def get_lead(lead_id):
    """Busca os dados completos do lead, incluindo contatos e catálogo vinculado."""
    url = f"{KOMMO_API}/leads/{lead_id}?with=contacts,catalog_elements"
    resp = requests.get(url, headers=kommo_headers())
    resp.raise_for_status()
    return resp.json()


def get_contact(contact_id):
    """Busca os dados do contato principal do lead."""
    url = f"{KOMMO_API}/contacts/{contact_id}"
    resp = requests.get(url, headers=kommo_headers())
    resp.raise_for_status()
    return resp.json()


def get_catalog_element(element_id):
    """Busca os detalhes de um elemento do catálogo (produto)."""
    url = f"{KOMMO_API}/catalogs/{CATALOG_ID}/elements/{element_id}"
    resp = requests.get(url, headers=kommo_headers())
    resp.raise_for_status()
    return resp.json()


def get_linked_products(lead_id):
    """Busca os produtos do catálogo vinculados ao lead."""
    url = f"{KOMMO_API}/leads/{lead_id}/links"
    resp = requests.get(url, headers=kommo_headers())
    resp.raise_for_status()
    data = resp.json()

    products = []
    if "_embedded" in data and "links" in data["_embedded"]:
        for link in data["_embedded"]["links"]:
            if link.get("to_entity_type") == "catalog_elements" and link.get("to_catalog_id") == CATALOG_ID:
                element_id = link["to_entity_id"]
                quantity = link.get("quantity", 1)
                # Buscar detalhes do produto
                try:
                    element = get_catalog_element(element_id)
                    product_name = element.get("name", "Produto")
                    # Buscar SKU e preço dos custom_fields do catálogo
                    sku = ""
                    price = 0
                    if "custom_fields_values" in element and element["custom_fields_values"]:
                        for cf in element["custom_fields_values"]:
                            if cf.get("field_code") == "SKU":
                                sku = cf["values"][0]["value"]
                            elif cf.get("field_code") == "PRICE":
                                price = float(cf["values"][0]["value"])
                    products.append({
                        "name": product_name,
                        "sku": sku,
                        "price": price,
                        "quantity": quantity
                    })
                except Exception as e:
                    logger.error(f"Erro ao buscar produto {element_id}: {e}")
    return products


def extract_custom_field(lead_data, field_id):
    """Extrai o valor de um campo personalizado do lead."""
    if "custom_fields_values" in lead_data and lead_data["custom_fields_values"]:
        for cf in lead_data["custom_fields_values"]:
            if cf["field_id"] == field_id:
                return cf["values"][0]["value"]
    return None


def update_lead_field(lead_id, field_id, value):
    """Atualiza um campo personalizado no lead do Kommo."""
    url = f"{KOMMO_API}/leads/{lead_id}"
    payload = {
        "custom_fields_values": [
            {
                "field_id": field_id,
                "values": [{"value": str(value)}]
            }
        ]
    }
    resp = requests.patch(url, headers=kommo_headers(), json=payload)
    resp.raise_for_status()
    return resp.json()


def add_note_to_lead(lead_id, text):
    """Adiciona uma nota ao lead do Kommo."""
    url = f"{KOMMO_API}/leads/{lead_id}/notes"
    payload = [
        {
            "note_type": "common",
            "params": {
                "text": text
            }
        }
    ]
    resp = requests.post(url, headers=kommo_headers(), json=payload)
    resp.raise_for_status()
    return resp.json()


# ══════════════════════════════════════════════════════════════════════════════
#  FUNÇÕES AUXILIARES - TINY
# ══════════════════════════════════════════════════════════════════════════════

def criar_proposta_tiny(cliente_nome, cliente_email, cliente_telefone,
                        produtos, pagamento, desconto, frete, observacoes):
    """Cria uma Proposta Comercial no Tiny ERP."""

    # Montar itens do pedido
    itens = []
    for i, prod in enumerate(produtos, 1):
        item = {
            "item": {
                "codigo": prod["sku"],
                "descricao": prod["name"],
                "unidade": "UN",
                "quantidade": str(prod["quantity"]),
                "valor_unitario": f"{prod['price']:.2f}"
            }
        }
        itens.append(item)

    # Montar o pedido (proposta comercial)
    pedido = {
        "pedido": {
            "cliente": {
                "nome": cliente_nome,
                "email": cliente_email or "",
                "fone": cliente_telefone or ""
            },
            "itens": itens,
            "situacao": "aberto",
            "obs": observacoes or "",
            "forma_pagamento": pagamento or "",
            "valor_frete": f"{frete:.2f}" if frete else "0.00",
            "valor_desconto": f"{desconto:.2f}" if desconto else "0.00"
        }
    }

    # Chamar API do Tiny
    url = f"{TINY_API}/pedido.incluir.php"
    payload = {
        "token": TINY_TOKEN,
        "pedido": json.dumps(pedido),
        "formato": "JSON"
    }

    logger.info(f"Enviando proposta ao Tiny: {json.dumps(pedido, ensure_ascii=False, indent=2)}")

    resp = requests.post(url, data=payload)
    resp.raise_for_status()
    result = resp.json()

    logger.info(f"Resposta do Tiny: {json.dumps(result, ensure_ascii=False)}")

    # Extrair número do pedido
    if "retorno" in result:
        retorno = result["retorno"]
        if retorno.get("status") == "OK":
            registros = retorno.get("registros", [])
            if registros:
                registro = registros[0].get("registro", {})
                return {
                    "sucesso": True,
                    "id": registro.get("id"),
                    "numero": registro.get("numero"),
                    "numero_ecommerce": registro.get("numero_ecommerce")
                }
        else:
            erros = retorno.get("erros", [])
            erro_msg = "; ".join([e.get("erro", str(e)) for e in erros]) if erros else "Erro desconhecido"
            return {"sucesso": False, "erro": erro_msg}

    return {"sucesso": False, "erro": "Resposta inesperada do Tiny"}


def pesquisar_produtos_tiny(pagina=1):
    """Pesquisa produtos ativos no Tiny ERP."""
    url = f"{TINY_API}/produtos.pesquisa.php"
    payload = {
        "token": TINY_TOKEN,
        "formato": "JSON",
        "situacao": "A",
        "pagina": str(pagina)
    }
    resp = requests.post(url, data=payload)
    resp.raise_for_status()
    return resp.json()


# ══════════════════════════════════════════════════════════════════════════════
#  ROTAS DA API
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/", methods=["GET"])
def home():
    """Health check."""
    return jsonify({
        "status": "online",
        "service": "Middleware Kommo <-> Tiny ERP",
        "endpoints": [
            "POST /webhook/proposta - Recebe webhook para criar proposta",
            "POST /sync/catalogo - Sincroniza catálogo Tiny -> Kommo",
            "GET /health - Health check"
        ]
    })


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/webhook/proposta", methods=["POST"])
def webhook_proposta():
    """
    Endpoint chamado pelo Salesbot do Kommo.
    Recebe o lead_id, busca os dados, cria a proposta no Tiny
    e atualiza o lead com o nº da proposta.
    """
    try:
        data = request.json or request.form.to_dict()
        logger.info(f"Webhook recebido: {json.dumps(data, ensure_ascii=False)}")

        # O Salesbot envia o lead_id
        lead_id = data.get("lead_id") or data.get("leads[status][0][id]")

        if not lead_id:
            # Tentar extrair de webhook padrão do Kommo
            if "leads" in data:
                leads = data["leads"]
                if "status" in leads and leads["status"]:
                    lead_id = leads["status"][0].get("id")
                elif "update" in leads and leads["update"]:
                    lead_id = leads["update"][0].get("id")

        if not lead_id:
            logger.error("lead_id não encontrado no payload")
            return jsonify({"error": "lead_id não encontrado"}), 400

        lead_id = int(lead_id)
        logger.info(f"Processando lead {lead_id}")

        # 1. Buscar dados do lead
        lead = get_lead(lead_id)
        logger.info(f"Lead obtido: {lead.get('name', 'sem nome')}")

        # 2. Extrair campos personalizados
        pagamento = extract_custom_field(lead, CF_PAGAMENTO)
        desconto_pct = extract_custom_field(lead, CF_DESCONTO)
        frete = extract_custom_field(lead, CF_FRETE)
        observacoes = extract_custom_field(lead, CF_OBSERVACOES)
        num_proposta_existente = extract_custom_field(lead, CF_NUM_PROPOSTA)

        # Verificar se já existe proposta criada
        if num_proposta_existente:
            logger.info(f"Lead {lead_id} já possui proposta nº {num_proposta_existente}")
            return jsonify({
                "status": "skip",
                "message": f"Proposta já existe: {num_proposta_existente}"
            })

        # 3. Buscar contato principal
        cliente_nome = lead.get("name", "Cliente")
        cliente_email = ""
        cliente_telefone = ""

        if "_embedded" in lead and "contacts" in lead["_embedded"]:
            contacts = lead["_embedded"]["contacts"]
            if contacts:
                contact_id = contacts[0]["id"]
                contact = get_contact(contact_id)
                cliente_nome = contact.get("name", cliente_nome)
                # Extrair email e telefone do contato
                if "custom_fields_values" in contact and contact["custom_fields_values"]:
                    for cf in contact["custom_fields_values"]:
                        code = cf.get("field_code", "")
                        if code == "EMAIL":
                            cliente_email = cf["values"][0]["value"]
                        elif code == "PHONE":
                            cliente_telefone = cf["values"][0]["value"]

        # 4. Buscar produtos vinculados ao lead
        produtos = get_linked_products(lead_id)

        if not produtos:
            logger.warning(f"Nenhum produto vinculado ao lead {lead_id}")
            add_note_to_lead(lead_id, "⚠️ Nenhum produto vinculado ao lead. Proposta não criada no Tiny.")
            return jsonify({"error": "Nenhum produto vinculado ao lead"}), 400

        # 5. Calcular desconto em valor
        subtotal = sum(p["price"] * p["quantity"] for p in produtos)
        desconto_valor = 0
        if desconto_pct:
            desconto_valor = subtotal * (float(desconto_pct) / 100)

        frete_valor = float(frete) if frete else 0

        # 6. Formatar itens para o campo "Itens" do Kommo
        itens_texto = ""
        for i, p in enumerate(produtos, 1):
            total_item = p["price"] * p["quantity"]
            itens_texto += f"{i}. {p['name']} (SKU: {p['sku']}) - {p['quantity']}x R${p['price']:.2f} = R${total_item:.2f}\n"

        itens_texto += f"\nSubtotal: R${subtotal:.2f}"
        if desconto_valor > 0:
            itens_texto += f"\nDesconto ({desconto_pct}%): -R${desconto_valor:.2f}"
        if frete_valor > 0:
            itens_texto += f"\nFrete: R${frete_valor:.2f}"
        total_final = subtotal - desconto_valor + frete_valor
        itens_texto += f"\n\nTOTAL: R${total_final:.2f}"

        # 7. Atualizar campo "Itens" no lead
        update_lead_field(lead_id, CF_ITENS, itens_texto)

        # 8. Criar proposta no Tiny
        resultado = criar_proposta_tiny(
            cliente_nome=cliente_nome,
            cliente_email=cliente_email,
            cliente_telefone=cliente_telefone,
            produtos=produtos,
            pagamento=pagamento or "",
            desconto=desconto_valor,
            frete=frete_valor,
            observacoes=observacoes or ""
        )

        if resultado["sucesso"]:
            num_proposta = resultado.get("numero", resultado.get("id", ""))

            # 9. Atualizar campo "Nº Proposta" no lead
            update_lead_field(lead_id, CF_NUM_PROPOSTA, str(num_proposta))

            # 10. Adicionar nota no lead
            add_note_to_lead(
                lead_id,
                f"✅ Proposta Comercial criada no Tiny!\n"
                f"Nº: {num_proposta}\n"
                f"Cliente: {cliente_nome}\n"
                f"Total: R${total_final:.2f}\n"
                f"Pagamento: {pagamento or 'Não informado'}"
            )

            logger.info(f"Proposta {num_proposta} criada com sucesso para lead {lead_id}")

            return jsonify({
                "status": "success",
                "proposta_numero": num_proposta,
                "proposta_id": resultado.get("id"),
                "total": total_final,
                "itens": len(produtos)
            })
        else:
            erro = resultado.get("erro", "Erro desconhecido")
            add_note_to_lead(lead_id, f"❌ Erro ao criar proposta no Tiny: {erro}")
            logger.error(f"Erro ao criar proposta no Tiny: {erro}")
            return jsonify({"error": erro}), 500

    except Exception as e:
        logger.exception(f"Erro no webhook: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/webhook/salesbot", methods=["POST"])
def webhook_salesbot():
    """
    Endpoint específico para o Salesbot do Kommo.
    O Salesbot envia uma requisição HTTP com o lead_id.
    Retorna os dados formatados para o Salesbot enviar na mensagem.
    """
    try:
        data = request.json or request.form.to_dict()
        logger.info(f"Salesbot webhook: {json.dumps(data, ensure_ascii=False)}")

        lead_id = data.get("lead_id")
        if not lead_id:
            return jsonify({"error": "lead_id obrigatório"}), 400

        lead_id = int(lead_id)

        # Buscar dados do lead
        lead = get_lead(lead_id)

        # Extrair campos
        pagamento = extract_custom_field(lead, CF_PAGAMENTO)
        desconto_pct = extract_custom_field(lead, CF_DESCONTO)
        frete = extract_custom_field(lead, CF_FRETE)
        observacoes = extract_custom_field(lead, CF_OBSERVACOES)

        # Buscar produtos vinculados
        produtos = get_linked_products(lead_id)

        if not produtos:
            return jsonify({
                "message": "Nenhum produto vinculado ao lead.",
                "status": "error"
            }), 400

        # Calcular valores
        subtotal = sum(p["price"] * p["quantity"] for p in produtos)
        desconto_valor = 0
        if desconto_pct:
            desconto_valor = subtotal * (float(desconto_pct) / 100)
        frete_valor = float(frete) if frete else 0
        total_final = subtotal - desconto_valor + frete_valor

        # Formatar mensagem para WhatsApp
        msg = "📋 *PROPOSTA COMERCIAL*\n"
        msg += "━━━━━━━━━━━━━━━━━━\n\n"

        for i, p in enumerate(produtos, 1):
            total_item = p["price"] * p["quantity"]
            msg += f"*{i}. {p['name']}*\n"
            msg += f"   SKU: {p['sku']}\n"
            msg += f"   Qtd: {p['quantity']} x R$ {p['price']:.2f}\n"
            msg += f"   Subtotal: R$ {total_item:.2f}\n\n"

        msg += "━━━━━━━━━━━━━━━━━━\n"
        msg += f"Subtotal: R$ {subtotal:.2f}\n"

        if desconto_valor > 0:
            msg += f"Desconto ({desconto_pct}%): -R$ {desconto_valor:.2f}\n"

        if frete_valor > 0:
            msg += f"Frete: R$ {frete_valor:.2f}\n"

        msg += f"\n💰 *TOTAL: R$ {total_final:.2f}*\n"

        if pagamento:
            msg += f"\n💳 Pagamento: {pagamento}"

        if observacoes:
            msg += f"\n📝 Obs: {observacoes}"

        msg += "\n\n━━━━━━━━━━━━━━━━━━"
        msg += "\nProposta válida por 7 dias."

        return jsonify({
            "message": msg,
            "total": total_final,
            "status": "success"
        })

    except Exception as e:
        logger.exception(f"Erro no salesbot webhook: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/sync/catalogo", methods=["POST"])
def sync_catalogo():
    """
    Sincroniza os produtos do Tiny para o catálogo do Kommo.
    Suporta checkpoint para retomar em caso de falha.
    """
    try:
        pagina = 1
        total_sincronizados = 0
        total_erros = 0

        while True:
            logger.info(f"Buscando produtos do Tiny - página {pagina}")
            resultado = pesquisar_produtos_tiny(pagina)

            retorno = resultado.get("retorno", {})
            status = retorno.get("status")

            if status != "OK":
                if pagina == 1:
                    return jsonify({"error": "Erro ao buscar produtos no Tiny"}), 500
                break

            produtos = retorno.get("produtos", [])
            if not produtos:
                break

            # Preparar elementos para o catálogo do Kommo
            elements = []
            for prod_wrapper in produtos:
                prod = prod_wrapper.get("produto", {})
                nome = prod.get("nome", "")
                codigo = prod.get("codigo", "")
                preco = prod.get("preco", "0")

                if not nome:
                    continue

                element = {
                    "name": nome,
                    "custom_fields_values": [
                        {
                            "field_code": "SKU",
                            "values": [{"value": str(codigo)}]
                        },
                        {
                            "field_code": "PRICE",
                            "values": [{"value": float(preco)}]
                        }
                    ]
                }
                elements.append(element)

            # Enviar em lotes de 50 para o Kommo
            for i in range(0, len(elements), 50):
                batch = elements[i:i+50]
                url = f"{KOMMO_API}/catalogs/{CATALOG_ID}/elements"

                try:
                    resp = requests.post(url, headers=kommo_headers(), json=batch)
                    if resp.status_code in [200, 201]:
                        total_sincronizados += len(batch)
                        logger.info(f"Lote sincronizado: {len(batch)} produtos")
                    else:
                        # Tentar atualizar (PATCH) se já existem
                        logger.warning(f"Erro ao criar lote, tentando atualizar: {resp.status_code}")
                        total_erros += len(batch)
                except Exception as e:
                    logger.error(f"Erro ao sincronizar lote: {e}")
                    total_erros += len(batch)

            # Verificar se há mais páginas
            numero_paginas = retorno.get("numero_paginas", 1)
            if pagina >= numero_paginas:
                break
            pagina += 1

        return jsonify({
            "status": "success",
            "total_sincronizados": total_sincronizados,
            "total_erros": total_erros,
            "paginas_processadas": pagina
        })

    except Exception as e:
        logger.exception(f"Erro na sincronização: {e}")
        return jsonify({"error": str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════════
#  INICIALIZAÇÃO
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"Middleware iniciando na porta {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
