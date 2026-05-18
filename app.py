from flask import Flask, request, jsonify
import io
import os

app = Flask(__name__)

def extract_text_from_pdf(file_bytes):
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            text = ""
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text += page_text + "\n"
        if text.strip():
            return text
        # Если текст пустой - скан, используем OCR
        return extract_text_ocr(file_bytes)
    except Exception as e:
        return f"Ошибка PDF: {str(e)}"

def extract_text_ocr(file_bytes):
    try:
        import pytesseract
        from pdf2image import convert_from_bytes
        images = convert_from_bytes(file_bytes)
        text = ""
        for image in images:
            text += pytesseract.image_to_string(image, lang='rus+eng') + "\n"
        return text
    except Exception as e:
        return f"Ошибка OCR: {str(e)}"

def extract_text_from_docx(file_bytes):
    try:
        from docx import Document
        doc = Document(io.BytesIO(file_bytes))
        text = ""
        for para in doc.paragraphs:
            if para.text.strip():
                text += para.text + "\n"
        # Также извлекаем текст из таблиц
        for table in doc.tables:
            for row in table.rows:
                for cell in row.cells:
                    if cell.text.strip():
                        text += cell.text + " | "
                text += "\n"
        return text
    except Exception as e:
        return f"Ошибка DOCX: {str(e)}"

def extract_text_from_xlsx(file_bytes):
    try:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
        text = ""
        for sheet in wb.worksheets:
            text += f"Лист: {sheet.title}\n"
            for row in sheet.iter_rows():
                row_text = []
                for cell in row:
                    if cell.value is not None:
                        row_text.append(str(cell.value))
                if row_text:
                    text += " | ".join(row_text) + "\n"
        return text
    except Exception as e:
        return f"Ошибка XLSX: {str(e)}"

@app.route('/parse', methods=['POST'])
def parse_document():
    if 'file' not in request.files:
        return jsonify({'error': 'Файл не найден'}), 400
    
    file = request.files['file']
    filename = file.filename.lower()
    file_bytes = file.read()
    
    if filename.endswith('.pdf'):
        text = extract_text_from_pdf(file_bytes)
    elif filename.endswith('.docx'):
        text = extract_text_from_docx(file_bytes)
    elif filename.endswith('.xlsx') or filename.endswith('.xls'):
        text = extract_text_from_xlsx(file_bytes)
    else:
        text = "Неподдерживаемый формат файла"
    
    # Ограничиваем размер текста для Claude
    if len(text) > 5000:
        text = text[:5000] + "...[текст обрезан]"
    
    return jsonify({'text': text, 'filename': file.filename})

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok'})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
