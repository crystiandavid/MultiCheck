from flask import Flask, render_template, request, jsonify, session
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
import json
import re
import pandas as pd
import io
import base64
import sqlite3
import os
import tempfile
import shutil

app = Flask(__name__)
app.secret_key = 'sua_chave_secreta_muito_segura_aqui_12345678'
app.config['BASE_DIR'] = 'sqlite:///checklist.db'
# app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///checklist.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024
db = SQLAlchemy(app)


HOST     = "127.0.0.1"
PORT     = 5000
URL      = f"http://{HOST}:{PORT}"
BASE_DIR = Path(__file__).parent.resolve()
HTML     = BASE_DIR / "index.html"

# ==================== MODELOS ====================

class Projeto(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(100), nullable=False, unique=True)
    descricao = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(20), default='Ativo')
    template_id = db.Column(db.Integer, db.ForeignKey('checklist_template_master.id'), nullable=True)
    data_criacao = db.Column(db.DateTime, default=datetime.utcnow)
    data_conclusao = db.Column(db.DateTime, nullable=True)

    empresas = db.relationship('Empresa', backref='projeto', lazy=True, cascade='all, delete-orphan')

    def to_dict(self):
        return {
            'id': self.id,
            'nome': self.nome,
            'descricao': self.descricao,
            'status': self.status,
            'template_id': self.template_id,
            'data_criacao': self.data_criacao.strftime('%d/%m/%Y %H:%M'),
            'data_conclusao': self.data_conclusao.strftime('%d/%m/%Y') if self.data_conclusao else None,
            'total_empresas': len(self.empresas),
            'empresas_concluidas': sum(1 for e in self.empresas if e.status == 'Concluído'),
            'progresso_medio': sum(e.progresso for e in self.empresas) / len(self.empresas) if self.empresas else 0
        }


class Empresa(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    projeto_id = db.Column(db.Integer, db.ForeignKey('projeto.id'), nullable=False)
    numero = db.Column(db.Integer, nullable=True)
    nome = db.Column(db.String(200), nullable=False)
    cnpj = db.Column(db.String(30), nullable=True)
    data_instalacao = db.Column(db.Date, nullable=True)
    progresso = db.Column(db.Float, default=0)
    status = db.Column(db.String(20), default='Pendente')
    data_conclusao = db.Column(db.Date, nullable=True)
    observacoes = db.Column(db.Text, nullable=True)

    progresso_itens = db.relationship('EmpresaProgresso', backref='empresa', lazy=True, cascade='all, delete-orphan')

    def to_dict(self):
        return {
            'id': self.id,
            'projeto_id': self.projeto_id,
            'numero': self.numero,
            'nome': self.nome,
            'cnpj': self.cnpj,
            'data_instalacao': self.data_instalacao.strftime('%d/%m/%Y') if self.data_instalacao else None,
            'progresso': self.progresso,
            'status': self.status,
            'data_conclusao': self.data_conclusao.strftime('%d/%m/%Y') if self.data_conclusao else None
        }


class ChecklistTemplateMaster(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(100), nullable=False, unique=True)
    descricao = db.Column(db.Text, nullable=True)
    data_criacao = db.Column(db.DateTime, default=datetime.utcnow)
    ativo = db.Column(db.Boolean, default=True)

    itens = db.relationship('ChecklistTemplateItem', backref='template', lazy=True, cascade='all, delete-orphan')
    projetos = db.relationship('Projeto', backref='template', lazy=True)

    def to_dict(self):
        return {
            'id': self.id,
            'nome': self.nome,
            'descricao': self.descricao,
            'data_criacao': self.data_criacao.strftime('%d/%m/%Y %H:%M'),
            'total_itens': len(self.itens),
            'ativo': self.ativo
        }


class ChecklistTemplateItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    template_id = db.Column(db.Integer, db.ForeignKey('checklist_template_master.id'), nullable=False)
    categoria = db.Column(db.String(100), nullable=False)
    categoria_ordem = db.Column(db.Integer, default=0)
    item_id = db.Column(db.String(20), nullable=False)
    descricao = db.Column(db.String(500), nullable=False)
    ordem = db.Column(db.Integer, default=0)
    obrigatorio = db.Column(db.Boolean, default=True)

    def to_dict(self):
        return {
            'id': self.id,
            'categoria': self.categoria,
            'categoria_ordem': self.categoria_ordem,
            'item_id': self.item_id,
            'descricao': self.descricao,
            'ordem': self.ordem,
            'obrigatorio': self.obrigatorio
        }


class EmpresaProgresso(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    empresa_id = db.Column(db.Integer, db.ForeignKey('empresa.id'), nullable=False)
    checklist_item_id = db.Column(db.Integer, db.ForeignKey('checklist_template_item.id'), nullable=False)
    concluido = db.Column(db.Boolean, default=False)
    observacao = db.Column(db.Text, nullable=True)
    data_atualizacao = db.Column(db.DateTime, default=datetime.utcnow)

    checklist_item = db.relationship('ChecklistTemplateItem')


# ==================== FUNÇÕES AUXILIARES ====================

def validar_cnpj(cnpj):
    if not cnpj:
        return True
    cnpj = re.sub(r'[^0-9]', '', cnpj)
    return len(cnpj) == 14


def validar_data(data_str):
    if not data_str:
        return True
    try:
        datetime.strptime(data_str, '%d/%m/%Y')
        return True
    except ValueError:
        return False


def recalcular_progresso_empresa(empresa_id):
    empresa = Empresa.query.get(empresa_id)
    if not empresa:
        return

    template_itens = ChecklistTemplateItem.query.join(ChecklistTemplateMaster).filter(
        ChecklistTemplateMaster.id == empresa.projeto.template_id
    ).all()

    total_itens = len(template_itens)

    if total_itens == 0:
        progresso = 0
    else:
        itens_concluidos = EmpresaProgresso.query.filter_by(empresa_id=empresa_id, concluido=True).count()
        progresso = (itens_concluidos / total_itens) * 100

    empresa.progresso = round(progresso, 2)

    if progresso == 100:
        empresa.status = 'Concluído'
        if not empresa.data_conclusao:
            empresa.data_conclusao = datetime.utcnow().date()
    elif progresso > 0:
        empresa.status = 'Em andamento'
        empresa.data_conclusao = None
    else:
        empresa.status = 'Pendente'
        empresa.data_conclusao = None

    db.session.commit()

    projeto = Projeto.query.get(empresa.projeto_id)
    if projeto:
        todas_empresas = Empresa.query.filter_by(projeto_id=projeto.id).all()
        if todas_empresas and all(e.status == 'Concluído' for e in todas_empresas):
            projeto.status = 'Concluído'
            projeto.data_conclusao = datetime.utcnow()
        elif any(e.status == 'Em andamento' for e in todas_empresas):
            projeto.status = 'Em andamento'
        else:
            projeto.status = 'Ativo'
        db.session.commit()


def criar_checklist_padrao():
    template = ChecklistTemplateMaster.query.filter_by(nome="Checklist Padrão POSLBC").first()
    if template:
        return template.id

    template = ChecklistTemplateMaster(
        nome="Checklist Padrão POSLBC",
        descricao="Checklist padrão para ativação de postos de combustível com sistema POSLBC"
    )
    db.session.add(template)
    db.session.commit()

    checklist_padrao = [
        ('1. CONFIGURAÇÃO DE FORMAS DE PAGAMENTO', 1, [
            ('1.1', 'Cartão de Crédito POSLBC criadas', 1),
            ('1.2', 'Cartão de Débito POSLBC criadas', 2),
            ('1.3', 'PIX POSLBC criado', 3),
            ('1.4', 'Formas POSLBC funcionando no POS', 4),
            ('1.5', 'Dinheiro DESATIVADA', 5),
            ('1.6', 'Pagamento Avulso DESATIVADA', 6),
        ]),
        ('2. CONFIGURAÇÃO DE CUPOM FISCAL', 2, [
            ('2.1', 'Impressão de cupom fiscal habilitada', 1),
            ('2.2', 'Emissão automática (máx 3 segundos)', 2),
            ('2.3', 'Impressão via emissão automática SUPRIMIDA', 3),
        ]),
        ('3. VALIDAÇÕES OPERACIONAIS', 3, [
            ('3.1', 'Teste venda Cartão (Crédito/Débito)', 1),
            ('3.2', 'Teste venda com PIX', 2),
            ('3.3', 'Pagamento identificado corretamente', 3),
        ]),
        ('4. ORIENTAÇÕES AO CLIENTE / EQUIPE', 4, [
            ('4.1', 'Divisão de abastecimento pagamento parcial', 1),
            ('4.2', 'Venda de produtos no cupom', 2),
            ('4.3', 'Baixa de pagamento via POS', 3),
            ('4.4', 'Não alterar formas POSLBC', 4),
        ]),
        ('5. INFRAESTRUTURA', 5, [
            ('5.1', 'XAMPP instalado', 1),
            ('5.2', 'XAMPP configurado como serviço Windows', 2),
            ('5.3', 'Serviços (Apache/MySQL) automáticos', 3),
        ]),
        ('6. VALIDAÇÃO FINAL', 6, [
            ('6.1', 'Teste completo ponta a ponta', 1),
            ('6.2', 'Funcionamento sem intervenção manual', 2),
            ('6.3', 'Ambiente validado e liberado', 3),
        ]),
    ]

    for categoria, cat_ordem, itens in checklist_padrao:
        for item_id, descricao, ordem in itens:
            item = ChecklistTemplateItem(
                template_id=template.id,
                categoria=categoria,
                categoria_ordem=cat_ordem,
                item_id=item_id,
                descricao=descricao,
                ordem=ordem,
                obrigatorio=True
            )
            db.session.add(item)

    db.session.commit()
    return template.id


# ==================== ROTAS ====================

@app.route('/')
def index():
    if HTML.exists():
        return send_file(str(HTML))
    return "<h2>index.html não encontrado</h2><p>Coloque o arquivo na mesma pasta que server.py</p>", 404


# ---------- PROJETOS ----------
@app.route('/api/projetos')
def api_projetos():
    projetos = Projeto.query.order_by(Projeto.data_criacao.desc()).all()
    return jsonify([p.to_dict() for p in projetos])


@app.route('/api/projeto', methods=['POST'])
def api_criar_projeto():
    data = request.json
    nome = data.get('nome', '').strip()
    descricao = data.get('descricao', '').strip()
    template_id = data.get('template_id')

    if not nome:
        return jsonify({'success': False, 'message': 'Nome do projeto é obrigatório'}), 400

    if Projeto.query.filter_by(nome=nome).first():
        return jsonify({'success': False, 'message': 'Já existe um projeto com este nome'}), 400

    if not template_id:
        template_id = criar_checklist_padrao()

    projeto = Projeto(nome=nome, descricao=descricao, template_id=template_id)
    db.session.add(projeto)
    db.session.commit()

    return jsonify({'success': True, 'projeto': projeto.to_dict()})




    if 'nome' in data:
        nome = data['nome'].strip()
        if Projeto.query.filter(Projeto.nome == nome, Projeto.id != projeto_id).first():
            return jsonify({'success': False, 'message': 'Nome já utilizado'}), 400
        projeto.nome = nome

    if 'descricao' in data:
        projeto.descricao = data['descricao']

    if 'status' in data:
        projeto.status = data['status']

    db.session.commit()
    return jsonify({'success': True, 'projeto': projeto.to_dict()})


@app.route('/api/projeto/<int:projeto_id>', methods=['DELETE'])
def api_deletar_projeto(projeto_id):
    projeto = Projeto.query.get_or_404(projeto_id)
    db.session.delete(projeto)
    db.session.commit()
    return jsonify({'success': True, 'message': 'Projeto removido'})


# ---------- EMPRESAS ----------
@app.route('/api/empresas/<int:projeto_id>')
def api_empresas(projeto_id):
    empresas = Empresa.query.filter_by(projeto_id=projeto_id).order_by(Empresa.numero).all()
    return jsonify([e.to_dict() for e in empresas])


@app.route('/api/empresa', methods=['POST'])
def api_criar_empresa():
    data = request.json
    projeto_id = data.get('projeto_id')

    if not projeto_id:
        return jsonify({'success': False, 'message': 'Projeto não informado'}), 400

    projeto = Projeto.query.get(projeto_id)
    if not projeto:
        return jsonify({'success': False, 'message': 'Projeto não encontrado'}), 404

    empresa = Empresa(
        projeto_id=projeto_id,
        numero=data.get('numero'),
        nome=data.get('nome', '').strip(),
        cnpj=data.get('cnpj'),
        data_instalacao=datetime.strptime(data['data_instalacao'], '%d/%m/%Y').date() if data.get(
            'data_instalacao') else None
    )

    db.session.add(empresa)
    db.session.commit()

    template_itens = ChecklistTemplateItem.query.filter_by(template_id=projeto.template_id).all()
    for item in template_itens:
        progresso = EmpresaProgresso(
            empresa_id=empresa.id,
            checklist_item_id=item.id,
            concluido=False
        )
        db.session.add(progresso)

    db.session.commit()

    return jsonify({'success': True, 'empresa': empresa.to_dict()})


@app.route('/api/empresas/importar', methods=['POST'])
def api_importar_empresas():
    data = request.json
    projeto_id = data.get('projeto_id')
    file_content = data.get('file_content')
    file_type = data.get('file_type', 'csv')

    if not projeto_id:
        return jsonify({'success': False, 'message': 'Projeto não informado'}), 400

    projeto = Projeto.query.get(projeto_id)
    if not projeto:
        return jsonify({'success': False, 'message': 'Projeto não encontrado'}), 404

    try:
        file_bytes = base64.b64decode(file_content.split(',')[1] if ',' in file_content else file_content)

        if file_type == 'csv':
            df = pd.read_csv(io.BytesIO(file_bytes))
        else:
            df = pd.read_excel(io.BytesIO(file_bytes))

        colunas = {col.lower(): col for col in df.columns}

        nome_col = None
        cnpj_col = None
        data_col = None
        numero_col = None

        for col in df.columns:
            col_lower = col.lower()
            if 'nome' in col_lower or 'razao' in col_lower:
                nome_col = col
            elif 'cnpj' in col_lower or 'documento' in col_lower:
                cnpj_col = col
            elif 'data' in col_lower or 'instalacao' in col_lower:
                data_col = col
            elif 'numero' in col_lower or 'codigo' in col_lower or '#' in col_lower:
                numero_col = col

        if not nome_col:
            return jsonify({'success': False, 'message': 'Arquivo não possui coluna de nome identificável'}), 400

        empresas_importadas = 0
        erros = []

        for idx, row in df.iterrows():
            try:
                nome = str(row[nome_col]) if pd.notna(row[nome_col]) else None
                if not nome:
                    erros.append(f"Linha {idx + 2}: Nome vazio")
                    continue

                numero = int(row[numero_col]) if numero_col and pd.notna(row[numero_col]) else None
                cnpj = str(row[cnpj_col]) if cnpj_col and pd.notna(row[cnpj_col]) else None
                data_instalacao = None

                if data_col and pd.notna(row[data_col]):
                    data_val = str(row[data_col])
                    try:
                        if '/' in data_val:
                            data_instalacao = datetime.strptime(data_val, '%d/%m/%Y').date()
                        elif '-' in data_val:
                            data_instalacao = datetime.strptime(data_val, '%Y-%m-%d').date()
                    except:
                        pass

                empresa = Empresa(
                    projeto_id=projeto_id,
                    numero=numero,
                    nome=nome[:200],
                    cnpj=cnpj[:30] if cnpj else None,
                    data_instalacao=data_instalacao
                )
                db.session.add(empresa)
                db.session.flush()

                template_itens = ChecklistTemplateItem.query.filter_by(template_id=projeto.template_id).all()
                for item in template_itens:
                    progresso = EmpresaProgresso(
                        empresa_id=empresa.id,
                        checklist_item_id=item.id,
                        concluido=False
                    )
                    db.session.add(progresso)

                empresas_importadas += 1

            except Exception as e:
                erros.append(f"Linha {idx + 2}: {str(e)}")

        db.session.commit()

        return jsonify({
            'success': True,
            'message': f'{empresas_importadas} empresas importadas com sucesso!',
            'erros': erros[:10]
        })

    except Exception as e:
        return jsonify({'success': False, 'message': f'Erro ao processar arquivo: {str(e)}'}), 400


@app.route('/api/empresa/<int:empresa_id>', methods=['PUT'])
def api_atualizar_empresa(empresa_id):
    empresa = Empresa.query.get_or_404(empresa_id)
    data = request.json

    if 'numero' in data:
        empresa.numero = data['numero']
    if 'nome' in data:
        empresa.nome = data['nome'].strip()
    if 'cnpj' in data:
        if data['cnpj'] and not validar_cnpj(data['cnpj']):
            return jsonify({'success': False, 'message': 'CNPJ inválido'}), 400
        empresa.cnpj = data['cnpj']
    if 'data_instalacao' in data:
        if data['data_instalacao'] and not validar_data(data['data_instalacao']):
            return jsonify({'success': False, 'message': 'Data inválida'}), 400
        empresa.data_instalacao = datetime.strptime(data['data_instalacao'], '%d/%m/%Y').date() if data[
            'data_instalacao'] else None
    if 'observacoes' in data:
        empresa.observacoes = data['observacoes']

    db.session.commit()
    return jsonify({'success': True, 'empresa': empresa.to_dict()})


@app.route('/api/empresa/<int:empresa_id>', methods=['DELETE'])
def api_deletar_empresa(empresa_id):
    empresa = Empresa.query.get_or_404(empresa_id)
    db.session.delete(empresa)
    db.session.commit()
    return jsonify({'success': True, 'message': 'Empresa removida'})


# ---------- TEMPLATES DE CHECKLIST ----------
@app.route('/api/templates')
def api_templates():
    templates = ChecklistTemplateMaster.query.filter_by(ativo=True).order_by(ChecklistTemplateMaster.nome).all()
    return jsonify([t.to_dict() for t in templates])


@app.route('/api/template', methods=['POST'])
def api_criar_template():
    data = request.json
    nome = data.get('nome', '').strip()
    descricao = data.get('descricao', '').strip()

    if not nome:
        return jsonify({'success': False, 'message': 'Nome do template é obrigatório'}), 400

    if ChecklistTemplateMaster.query.filter_by(nome=nome).first():
        return jsonify({'success': False, 'message': 'Template já existe'}), 400

    template = ChecklistTemplateMaster(nome=nome, descricao=descricao)
    db.session.add(template)
    db.session.commit()

    return jsonify({'success': True, 'template': template.to_dict()})


@app.route('/api/template/<int:template_id>/duplicar', methods=['POST'])
def api_duplicar_template(template_id):
    template_original = ChecklistTemplateMaster.query.get_or_404(template_id)
    data = request.json
    novo_nome = data.get('novo_nome', f"{template_original.nome} (Cópia)")

    if ChecklistTemplateMaster.query.filter_by(nome=novo_nome).first():
        return jsonify({'success': False, 'message': 'Já existe um template com este nome'}), 400

    # Criar novo template
    novo_template = ChecklistTemplateMaster(
        nome=novo_nome,
        descricao=f"Cópia de: {template_original.nome}\n{template_original.descricao or ''}",
        ativo=True
    )
    db.session.add(novo_template)
    db.session.commit()

    # Copiar itens
    for item in template_original.itens:
        novo_item = ChecklistTemplateItem(
            template_id=novo_template.id,
            categoria=item.categoria,
            categoria_ordem=item.categoria_ordem,
            item_id=item.item_id,
            descricao=item.descricao,
            ordem=item.ordem,
            obrigatorio=item.obrigatorio
        )
        db.session.add(novo_item)

    db.session.commit()

    return jsonify({'success': True, 'template': novo_template.to_dict()})


@app.route('/api/template/<int:template_id>', methods=['PUT'])
def api_atualizar_template(template_id):
    template = ChecklistTemplateMaster.query.get_or_404(template_id)
    data = request.json

    if 'nome' in data:
        nome = data['nome'].strip()
        if ChecklistTemplateMaster.query.filter(ChecklistTemplateMaster.nome == nome,
                                                ChecklistTemplateMaster.id != template_id).first():
            return jsonify({'success': False, 'message': 'Nome já utilizado'}), 400
        template.nome = nome

    if 'descricao' in data:
        template.descricao = data['descricao']

    db.session.commit()
    return jsonify({'success': True, 'template': template.to_dict()})


@app.route('/api/template/<int:template_id>/itens', methods=['GET'])
def api_template_itens(template_id):
    itens = ChecklistTemplateItem.query.filter_by(template_id=template_id).order_by(
        ChecklistTemplateItem.categoria_ordem, ChecklistTemplateItem.ordem
    ).all()
    return jsonify([i.to_dict() for i in itens])


@app.route('/api/template/<int:template_id>/categoria', methods=['POST'])
def api_adicionar_categoria(template_id):
    data = request.json
    template = ChecklistTemplateMaster.query.get_or_404(template_id)

    categoria = data.get('categoria', '').strip()
    if not categoria:
        return jsonify({'success': False, 'message': 'Nome da categoria é obrigatório'}), 400

    # Verificar quantas categorias já existem
    existing_categorias = {item.categoria for item in template.itens}
    if categoria in existing_categorias:
        return jsonify({'success': False, 'message': 'Categoria já existe'}), 400

    proxima_ordem = len([i for i in template.itens if i.categoria == categoria]) + 1

    return jsonify({'success': True, 'categoria': categoria, 'ordem': proxima_ordem})


@app.route('/api/template/<int:template_id>/item', methods=['POST'])
def api_adicionar_item(template_id):
    data = request.json
    template = ChecklistTemplateMaster.query.get_or_404(template_id)

    categoria = data.get('categoria', '').strip()
    item_id = data.get('item_id', '').strip()
    descricao = data.get('descricao', '').strip()

    if not categoria:
        return jsonify({'success': False, 'message': 'Categoria é obrigatória'}), 400
    if not item_id:
        return jsonify({'success': False, 'message': 'ID do item é obrigatório'}), 400
    if not descricao:
        return jsonify({'success': False, 'message': 'Descrição do item é obrigatória'}), 400

    # Calcular ordem da categoria
    categoria_ordem = 1
    existing_categorias = {}
    for item in template.itens:
        if item.categoria not in existing_categorias:
            existing_categorias[item.categoria] = item.categoria_ordem

    if categoria not in existing_categorias:
        categoria_ordem = len(existing_categorias) + 1
    else:
        categoria_ordem = existing_categorias[categoria]

    # Calcular ordem do item na categoria
    itens_na_categoria = [i for i in template.itens if i.categoria == categoria]
    ordem = len(itens_na_categoria) + 1

    novo_item = ChecklistTemplateItem(
        template_id=template_id,
        categoria=categoria,
        categoria_ordem=categoria_ordem,
        item_id=item_id,
        descricao=descricao,
        ordem=ordem,
        obrigatorio=data.get('obrigatorio', True)
    )
    db.session.add(novo_item)
    db.session.commit()

    return jsonify({'success': True, 'item': novo_item.to_dict()})


@app.route('/api/template/item/<int:item_id>', methods=['PUT'])
def api_atualizar_item(item_id):
    item = ChecklistTemplateItem.query.get_or_404(item_id)
    data = request.json

    if 'descricao' in data:
        item.descricao = data['descricao']
    if 'obrigatorio' in data:
        item.obrigatorio = data['obrigatorio']

    db.session.commit()
    return jsonify({'success': True, 'item': item.to_dict()})


@app.route('/api/template/item/<int:item_id>', methods=['DELETE'])
def api_deletar_item(item_id):
    item = ChecklistTemplateItem.query.get_or_404(item_id)
    template_id = item.template_id

    # Reordenar itens da mesma categoria
    itens_mesma_categoria = ChecklistTemplateItem.query.filter_by(
        template_id=template_id, categoria=item.categoria
    ).order_by(ChecklistTemplateItem.ordem).all()

    for i, it in enumerate(itens_mesma_categoria, 1):
        if it.ordem > item.ordem:
            it.ordem -= 1

    db.session.delete(item)
    db.session.commit()

    return jsonify({'success': True, 'message': 'Item removido'})


@app.route('/api/template/<int:template_id>', methods=['DELETE'])
def api_deletar_template(template_id):
    template = ChecklistTemplateMaster.query.get_or_404(template_id)
    db.session.delete(template)
    db.session.commit()
    return jsonify({'success': True, 'message': 'Template removido'})


# ---------- CHECKLIST ----------
@app.route('/api/checklist/template/<int:template_id>')
def api_checklist_template(template_id):
    itens = ChecklistTemplateItem.query.filter_by(template_id=template_id).order_by(
        ChecklistTemplateItem.categoria_ordem, ChecklistTemplateItem.ordem
    ).all()

    categorias = {}
    for item in itens:
        if item.categoria not in categorias:
            categorias[item.categoria] = []
        categorias[item.categoria].append(item.to_dict())

    return jsonify(categorias)


@app.route('/api/checklist/empresa/<int:empresa_id>')
def api_checklist_empresa(empresa_id):
    empresa = Empresa.query.get_or_404(empresa_id)
    template_itens = ChecklistTemplateItem.query.filter_by(template_id=empresa.projeto.template_id).order_by(
        ChecklistTemplateItem.categoria_ordem, ChecklistTemplateItem.ordem
    ).all()

    progressos = {p.checklist_item_id: p for p in EmpresaProgresso.query.filter_by(empresa_id=empresa_id).all()}

    resultado = []
    for item in template_itens:
        progresso = progressos.get(item.id)
        resultado.append({
            'checklist_item_id': item.id,
            'item_id': item.item_id,
            'categoria': item.categoria,
            'descricao': item.descricao,
            'concluido': progresso.concluido if progresso else False,
            'observacao': progresso.observacao if progresso else '',
            'obrigatorio': item.obrigatorio
        })

    return jsonify(resultado)


@app.route('/api/checklist/salvar', methods=['POST'])
def api_salvar_checklist():
    data = request.json
    empresa_id = data['empresa_id']
    checklist_item_id = data['checklist_item_id']
    concluido = data['concluido']
    observacao = data.get('observacao', '')

    progresso = EmpresaProgresso.query.filter_by(empresa_id=empresa_id, checklist_item_id=checklist_item_id).first()
    if progresso:
        progresso.concluido = concluido
        progresso.observacao = observacao
        progresso.data_atualizacao = datetime.utcnow()
    else:
        progresso = EmpresaProgresso(
            empresa_id=empresa_id,
            checklist_item_id=checklist_item_id,
            concluido=concluido,
            observacao=observacao
        )
        db.session.add(progresso)

    db.session.commit()
    recalcular_progresso_empresa(empresa_id)

    empresa = Empresa.query.get(empresa_id)

    return jsonify({
        'success': True,
        'progresso': empresa.progresso,
        'status': empresa.status
    })


# ---------- MIGRAÇÃO ----------
@app.route('/api/migrar/selecionar', methods=['POST'])
def api_migrar_selecionar_banco():
    data = request.json
    file_content = data.get('file_content')
    file_name = data.get('file_name', 'database.db')

    if not file_content:
        return jsonify({'success': False, 'message': 'Arquivo não enviado'}), 400

    try:
        file_bytes = base64.b64decode(file_content.split(',')[1] if ',' in file_content else file_content)

        session_id = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
        temp_dir = os.path.join(tempfile.gettempdir(), f'migracao_{session_id}')
        os.makedirs(temp_dir, exist_ok=True)

        temp_db_path = os.path.join(temp_dir, file_name)

        with open(temp_db_path, 'wb') as f:
            f.write(file_bytes)

        conn = sqlite3.connect(temp_db_path)
        cursor = conn.cursor()

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='loja'")
        tem_loja = cursor.fetchone() is not None

        if not tem_loja:
            shutil.rmtree(temp_dir, ignore_errors=True)
            return jsonify(
                {'success': False, 'message': 'Banco de dados não contém a tabela "loja" (formato antigo)'}), 400

        cursor.execute("SELECT COUNT(*) FROM loja")
        total_lojas = cursor.fetchone()[0]

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='progresso_item'")
        tem_progresso = cursor.fetchone() is not None

        total_progressos = 0
        if tem_progresso:
            cursor.execute("SELECT COUNT(*) FROM progresso_item")
            total_progressos = cursor.fetchone()[0]

        cursor.execute("SELECT MIN(data_instalacao) FROM loja")
        mais_antiga = cursor.fetchone()
        data_mais_antiga = mais_antiga[0] if mais_antiga and mais_antiga[0] else None

        cursor.execute("SELECT id, numero, razao_social, cnpj, data_instalacao FROM loja LIMIT 5")
        empresas_preview = []
        for row in cursor.fetchall():
            empresas_preview.append({
                'id': row[0],
                'numero': row[1],
                'nome': row[2],
                'cnpj': row[3],
                'data': row[4]
            })

        conn.close()

        session['migracao_temp_dir'] = temp_dir
        session['migracao_db_path'] = temp_db_path

        return jsonify({
            'success': True,
            'info': {
                'total_lojas': total_lojas,
                'total_progressos': total_progressos,
                'tem_progresso': tem_progresso,
                'data_mais_antiga': data_mais_antiga,
                'arquivo': file_name,
                'empresas_preview': empresas_preview
            }
        })

    except Exception as e:
        return jsonify({'success': False, 'message': f'Erro ao analisar banco: {str(e)}'}), 400


@app.route('/api/migrar/executar', methods=['POST'])
def api_migrar_executar():
    data = request.json
    projeto_nome = data.get('projeto_nome', 'Projeto Migrado')

    temp_dir = session.get('migracao_temp_dir')
    db_path = session.get('migracao_db_path')

    if not db_path or not os.path.exists(db_path):
        return jsonify({'success': False,
                        'message': 'Arquivo de banco não encontrado. Por favor, selecione o arquivo novamente.'}), 400

    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        cursor.execute(
            "SELECT id, numero, data_instalacao, cnpj, razao_social, progresso, status, data_conclusao FROM loja")
        lojas_antigas = cursor.fetchall()

        if not lojas_antigas:
            conn.close()
            return jsonify({'success': False, 'message': 'Nenhuma loja encontrada'}), 400

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='progresso_item'")
        tem_progresso = cursor.fetchone() is not None

        progressos_antigos = []
        if tem_progresso:
            cursor.execute("SELECT loja_id, item_id, concluido, observacao FROM progresso_item")
            progressos_antigos = cursor.fetchall()

        conn.close()

        template_id = criar_checklist_padrao()

        projeto = Projeto(
            nome=projeto_nome,
            descricao=f"Projeto migrado contendo {len(lojas_antigas)} empresas",
            template_id=template_id,
            status="Em andamento"
        )
        db.session.add(projeto)
        db.session.commit()

        mapa_empresas = {}

        for loja in lojas_antigas:
            empresa = Empresa(
                projeto_id=projeto.id,
                numero=loja[1],
                nome=loja[4],
                cnpj=loja[3],
                data_instalacao=datetime.strptime(loja[2], '%Y-%m-%d').date() if loja[2] else None,
                progresso=loja[5] or 0,
                status=loja[6] or 'Pendente',
                data_conclusao=datetime.strptime(loja[7], '%Y-%m-%d').date() if loja[7] else None
            )
            db.session.add(empresa)
            db.session.flush()
            mapa_empresas[loja[0]] = empresa.id

        db.session.commit()

        template_itens = {item.item_id: item.id for item in
                          ChecklistTemplateItem.query.filter_by(template_id=template_id).all()}

        for progresso in progressos_antigos:
            empresa_nova_id = mapa_empresas.get(progresso[0])
            if empresa_nova_id and progresso[1] in template_itens:
                novo_progresso = EmpresaProgresso(
                    empresa_id=empresa_nova_id,
                    checklist_item_id=template_itens[progresso[1]],
                    concluido=bool(progresso[2]),
                    observacao=progresso[3] if len(progresso) > 3 else ''
                )
                db.session.add(novo_progresso)

        db.session.commit()

        if temp_dir and os.path.exists(temp_dir):
            try:
                shutil.rmtree(temp_dir, ignore_errors=True)
            except:
                pass

        session.pop('migracao_temp_dir', None)
        session.pop('migracao_db_path', None)

        return jsonify({
            'success': True,
            'message': f'Migração concluída! {len(lojas_antigas)} empresas migradas para o projeto "{projeto_nome}"',
            'projeto_id': projeto.id
        })

    except Exception as e:
        return jsonify({'success': False, 'message': f'Erro na migração: {str(e)}'}), 400


# ---------- DASHBOARD ----------
@app.route('/api/dashboard')
def api_dashboard():
    projetos = Projeto.query.all()
    total_projetos = len(projetos)
    ativos = sum(1 for p in projetos if p.status == 'Ativo')
    em_andamento = sum(1 for p in projetos if p.status == 'Em andamento')
    concluidos = sum(1 for p in projetos if p.status == 'Concluído')

    return jsonify({
        'total_projetos': total_projetos,
        'ativos': ativos,
        'em_andamento': em_andamento,
        'concluidos': concluidos
    })


# ==================== INICIALIZAÇÃO ====================
with app.app_context():
    db.create_all()
    criar_checklist_padrao()
    print("✅ Sistema iniciado com sucesso!")

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
