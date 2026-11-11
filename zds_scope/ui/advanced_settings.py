"""Human-facing forms for settings already read from the instrument."""
from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QComboBox,
                              QPushButton, QScrollArea, QFormLayout, QDoubleSpinBox)
from ..instrument_settings import GLOBAL, CHANNEL

MODES = [('边沿','EDGE'),('脉宽','PULSE'),('斜率','SLOPE'),('视频','VIDEO'),
         ('欠幅','RUNT'),('预欠幅','PRUNT'),('码型','PATTERN'),('第 N 边沿','NEDGE'),
         ('延迟','DELAY'),('超时','TIMEOUT'),('建立 / 保持','SHOLD'),('A 到 B','ATOBN'),('交替','ALTER')]
NAMES = {'SOURCE':'触发源','SLOPE':'边沿方向','LEVEL':'触发电平','WHEN':'判定条件',
         'UWIDTH':'脉宽上限','LWIDTH':'脉宽下限','TUPPER':'时间上限','TLOWER':'时间下限',
         'HLEVEL':'上电平','LLEVEL':'下电平','WINDOW':'电平窗口','ASRC':'A 来源','BSRC':'B 来源',
         'CSRC':'C 来源','DSRC':'D 来源','APAT':'A 码型','BPAT':'B 码型','POLARITY':'极性',
         'STANDARD':'视频制式','LINE':'视频行号','EDGENUM':'边沿计数','IDLE':'空闲时间',
         'TIME':'时间','STIME':'建立时间','HTIME':'保持时间','ATHRES':'A 阈值','BTHRES':'B 阈值'}

class AdvancedSettings(QWidget):
    write_requested = Signal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.values, self.configuration, self.editors = {}, {}, {}
        self.connected, self.prefixes = False, None
        layout = QVBoxLayout(self)
        self.status = QLabel('连接设备后显示当前设置。')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        self.apply = QWidget()
        self.form = QFormLayout(self.apply)
        self.form.setSpacing(12)
        scroll.setWidget(self.apply); layout.addWidget(scroll)

    def _options(self, command):
        key = command.upper(); suffix = key.rsplit(':',1)[-1]
        choices = {
            ':TRIGGER:MODE': MODES,
            ':TRIGGER:SWEEP': [('自动','AUTO'),('正常','NORMAL')],
            ':TRIGGER:COUPLING': [('直流','DC'),('交流','AC'),('低频抑制','LFREJECT'),('高频抑制','HFREJECT')],
            ':ACQUIRE:TYPE': [('标准采样','NORMAL'),('峰值检测','PEAK'),('平均','AVERAGES'),('高分辨率','HRESOLUTION')],
            ':ACQUIRE:MAREA': [('自动','AUTO'),('固定','FIXED')],
            ':ACQUIRE:AVERAGES': [(str(2**n),str(2**n)) for n in range(1,17)],
        }
        if key in choices: return choices[key]
        if suffix in ('SOURCE','ASRC','BSRC','CSRC','DSRC'):
            result = [(f'CH{ch}',f'CHANNEL{ch}') for ch in range(1,5)]
            if ':EDGE:' in key: result += [('电源','LINE'),('外部','EXTERNAL')]
            return result
        if suffix == 'UNITS': return [('电压 V','VOLTAGE'),('电流 A','AMPERE')]
        if suffix == 'TERMINATION': return [('1 MΩ','1M'),('50 Ω','50')]
        if suffix == 'INVERT': return [('正常','0'),('反相','1')]
        if suffix == 'PROBE':
            current = self.values.get(command.rsplit(':',1)[0]+':UNITs','VOLTAGE').upper()
            values = [10,5,2,1,.5,.2,.1,.05,.02,.01,.005,.002,.001] if current == 'AMPERE' else [.1,.2,.5,1,2,5,10,20,50,100,200,500,1000]
            return [(f'{v:g}',f'{v:g}') for v in values]
        if suffix == 'SLOPE' and ':EDGE:' in key:
            return [('上升沿','POSITIVE'),('下降沿','NEGATIVE'),('任意边沿','EITHER')]
        if suffix == 'WHEN' and (':PULSE:' in key or ':SLOPE:' in key):
            return [('正向大于','PGREATER'),('正向小于','PLESS'),('正向范围内','PGLESS'),
                    ('负向大于','NGREATER'),('负向小于','NLESS'),('负向范围内','NGLESS')]
        return []

    def _editor(self, command, raw):
        options = self._options(command)
        if options:
            widget = QComboBox()
            for label, value in options: widget.addItem(label,value)
            normalized = str(raw).strip().upper()
            if command.upper().endswith(':INVERT'): normalized = {'ON':'1','OFF':'0'}.get(normalized,normalized)
            match = next((i for i,(_,v) in enumerate(options) if v.upper()==normalized),-1)
            if match < 0:
                widget.addItem(f'当前：{raw}',str(raw));match=widget.count()-1
            widget.setCurrentIndex(match)
            return widget, lambda: str(widget.currentData())
        suffix = command.upper().rsplit(':',1)[-1]
        time_fields = {'HOLDOFF','DELAY','UWIDTH','LWIDTH','TUPPER','TLOWER','TIME','STIME','HTIME','IDLE'}
        if suffix in time_fields or suffix in {'LEVEL','HLEVEL','LLEVEL','ATHRES','BTHRES','EDGENUM','LINE'}:
            try: number=float(raw)
            except (ValueError,TypeError): return QLabel('当前值不可用'), None
            factor, unit = 1.0, ''
            if suffix in time_fields:
                factor,unit = ((1e9,' ns') if abs(number)<1e-6 else (1e6,' µs') if abs(number)<1e-3 else (1e3,' ms') if abs(number)<1 else (1,' s'))
            elif suffix not in {'EDGENUM','LINE'}:
                source = self.values.get(command.rsplit(':',1)[0]+':SOURce','CHANNEL1').upper()
                unit = ' A' if self.values.get(f':CHANnel{source[-1:]}:UNITs','').upper()=='AMPERE' else ' V'
            widget=QDoubleSpinBox();widget.setDecimals(6);widget.setKeyboardTracking(False)
            low, high = -1e12, 1e12
            if suffix in time_fields: low=0
            if suffix=='DELAY': low,high=-100e-9*factor,100e-9*factor
            if suffix=='HOLDOFF': high=16*factor
            if suffix in {'UWIDTH','LWIDTH','TUPPER','TLOWER'}: low,high=1e-9*factor,1*factor
            if suffix in {'EDGENUM','LINE'}: low=1;widget.setDecimals(0)
            widget.setRange(low,high);widget.setSuffix(unit);widget.setValue(number*factor)
            if suffix in time_fields:
                container = QWidget(); row = QHBoxLayout(container); row.setContentsMargins(0,0,0,0)
                unit_box = QComboBox()
                for label, scale in [('ns',1e9),('µs',1e6),('ms',1e3),('s',1.0)]: unit_box.addItem(label,scale)
                unit_box.setCurrentIndex(next(i for i in range(4) if unit_box.itemData(i)==factor))
                current_factor = [factor]
                widget.setSuffix('')
                def change_unit(index):
                    seconds = widget.value()/current_factor[0]
                    scale = unit_box.currentData()
                    widget.setRange(low/factor*scale,high/factor*scale)
                    widget.setValue(seconds*scale)
                    current_factor[0]=scale
                unit_box.currentIndexChanged.connect(change_unit)
                row.addWidget(widget,1);row.addWidget(unit_box)
                return container, lambda: format(widget.value()/unit_box.currentData(),'.15g')
            return widget, lambda: format(widget.value()/factor,'.15g')
        # Unmapped specialist parameters remain visible, with no free-form commands.
        return QLabel(str(raw)), None

    def set_configuration(self, configuration, connected=False):
        self.configuration=configuration;self.values=configuration.get('extended_settings',{});self.connected=connected
        self.editors={}
        while self.form.rowCount(): self.form.removeRow(0)
        visible=[(k,v) for k,v in self.values.items() if self.prefixes is None or k.upper().startswith(self.prefixes)]
        for command, raw in visible:
            if command==':TRIG:STATE': continue
            if command.upper()==':ACQUIRE:AVERAGES' and str(self.values.get(':ACQuire:TYPE','')).upper()!='AVERAGES': continue
            suffix=command.rsplit(':',1)[-1]
            label=GLOBAL.get(command,CHANNEL.get(suffix,NAMES.get(suffix.upper(),'其他设置'))).replace('(s)','')
            widget,get_value=self._editor(command,raw)
            row=QWidget(); row_layout=QHBoxLayout(row);row_layout.setContentsMargins(0,0,0,0)
            row_layout.addWidget(widget,1)
            if get_value:
                button=QPushButton('设置');button.setFixedWidth(54)
                button.clicked.connect(lambda checked=False,c=command,get=get_value: self.write_requested.emit(c,get()) if self.connected else None)
                row_layout.addWidget(button)
            self.form.addRow(label,row);self.editors[command]=widget
        self.apply.setEnabled(connected)
        state=str(self.values.get(':TRIG:STATE',''))
        translated={'IFTRIG':'等待触发','STOP':'停止','RUN':'运行','TRIGGED':'已触发'}.get(state.upper(),state)
        self.status.setText(('当前设备设置' if connected else '离线设置') + (f' · {translated}' if translated else '') + '\n修改所需项目后点击该行“设置”。')
        if not visible:self.status.setText('连接设备后显示当前设置。')

    def set_filter(self,prefixes):
        self.prefixes=tuple(p.upper() for p in prefixes) if prefixes else None
        self.set_configuration(self.configuration,self.connected)
