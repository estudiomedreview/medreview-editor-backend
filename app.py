import gradio as gr

def test(video, nome):
    if not video:
        return None, "Nenhum vídeo enviado"
    return video, f"✅ Recebido: {nome} | Arquivo: {video}"

demo = gr.Interface(
    fn=test,
    inputs=[
        gr.Video(label="Vídeo"),
        gr.Textbox(label="Nome", value="Teste"),
    ],
    outputs=[
        gr.Video(label="Output"),
        gr.Textbox(label="Status"),
    ],
    title="Teste MED-Review",
)
demo.launch()
