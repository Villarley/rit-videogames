package rit;

import java.io.IOException;
import java.io.Reader;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.text.ParseException;
import java.util.ArrayList;
import java.util.List;
import java.util.regex.Pattern;
import org.apache.lucene.analysis.Analyzer;
import org.apache.lucene.analysis.CharArraySet;
import org.apache.lucene.analysis.LowerCaseFilter;
import org.apache.lucene.analysis.StopFilter;
import org.apache.lucene.analysis.TokenStream;
import org.apache.lucene.analysis.Tokenizer;
import org.apache.lucene.analysis.charfilter.MappingCharFilter;
import org.apache.lucene.analysis.charfilter.NormalizeCharMap;
import org.apache.lucene.analysis.core.FlattenGraphFilter;
import org.apache.lucene.analysis.en.PorterStemFilter;
import org.apache.lucene.analysis.miscellaneous.LengthFilter;
import org.apache.lucene.analysis.pattern.PatternTokenizer;
import org.apache.lucene.analysis.synonym.SolrSynonymParser;
import org.apache.lucene.analysis.synonym.SynonymGraphFilter;
import org.apache.lucene.analysis.synonym.SynonymMap;

/**
 * Las mismas etapas y en el mismo orden que indexer/normalizacion.py:
 *
 * <pre>
 * NormalizadorCharFilter (fechas, números)  -> MappingCharFilter (plegado, config/plegado.txt)
 * -> PatternTokenizer [\p{L}\p{N}]+ -> LowerCaseFilter -> LengthFilter
 * -> SynonymGraphFilter (config/nombres.txt + config/tesauro.txt) -> FlattenGraphFilter
 * -> StopFilter (config/stopwords.txt) -> PorterStemFilter
 * </pre>
 *
 * Todas son piezas de Lucene salvo el CharFilter de fechas y números.
 */
public final class AnalizadorRit extends Analyzer {
    public static final Pattern TOKEN = Pattern.compile("[\\p{L}\\p{N}]+");

    private final boolean fechas;
    private final boolean numeros;
    private final boolean minusculas;
    private final NormalizeCharMap plegado;
    private final CharArraySet stop;
    private final SynonymMap sinonimos;
    private final boolean porter;
    private final int largoMin;
    private final int largoMax;
    public final String resumen;

    public AnalizadorRit(Configuracion c) throws IOException {
        fechas = c.booleano("normalizacion", "fechas", false);
        numeros = c.booleano("normalizacion", "numeros", false);
        minusculas = c.booleano("normalizacion", "minusculas", true);
        Path rPlegado = c.ruta("normalizacion", "plegado", "no");
        plegado = rPlegado == null ? null : leerPlegado(rPlegado);
        Path rStop = c.ruta("normalizacion", "stopwords", "no");
        stop = rStop == null ? null : leerLista(rStop);
        List<Path> reglas = new ArrayList<>();
        Path rNombres = c.ruta("normalizacion", "nombres", "no");
        Path rTesauro = c.ruta("normalizacion", "tesauro", "no");
        if (rNombres != null) reglas.add(rNombres);
        if (rTesauro != null) reglas.add(rTesauro);
        sinonimos = reglas.isEmpty() ? null : leerSinonimos(reglas, plegado, minusculas);
        porter = c.texto("normalizacion", "stemming", "no").equalsIgnoreCase("porter");
        largoMin = c.entero("normalizacion", "largo_min", 1);
        largoMax = c.entero("normalizacion", "largo_max", 40);
        List<String> partes = new ArrayList<>();
        if (plegado != null) partes.add("plegado");
        if (minusculas) partes.add("minusculas");
        if (fechas) partes.add("fechas");
        if (numeros) partes.add("numeros");
        if (rNombres != null) partes.add("nombres");
        if (rTesauro != null) partes.add("tesauro");
        if (stop != null) partes.add("stopwords");
        if (porter) partes.add("porter");
        resumen = String.join("+", partes);
    }

    static NormalizeCharMap leerPlegado(Path p) throws IOException {
        NormalizeCharMap.Builder b = new NormalizeCharMap.Builder();
        for (String linea : Files.readAllLines(p, StandardCharsets.UTF_8)) {
            linea = linea.strip();
            if (linea.isEmpty() || linea.startsWith("#")) continue;
            String[] lados = linea.split("=>");
            b.add(sinComillas(lados[0]), sinComillas(lados[1]));
        }
        return b.build();
    }

    private static String sinComillas(String s) {
        s = s.strip();
        return s.substring(1, s.length() - 1);
    }

    static CharArraySet leerLista(Path p) throws IOException {
        CharArraySet set = new CharArraySet(512, false);
        for (String linea : Files.readAllLines(p, StandardCharsets.UTF_8)) {
            linea = linea.strip();
            if (!linea.isEmpty() && !linea.startsWith("#")) set.add(linea);
        }
        return set;
    }

    /** Las reglas se analizan con plegado + tokens + minúsculas, igual que en Python. */
    static SynonymMap leerSinonimos(List<Path> archivos, NormalizeCharMap plegado, boolean minusculas)
            throws IOException {
        Analyzer reglas = new Analyzer() {
            @Override
            protected TokenStreamComponents createComponents(String campo) {
                Tokenizer t = new PatternTokenizer(TOKEN, 0);
                TokenStream s = minusculas ? new LowerCaseFilter(t) : t;
                return new TokenStreamComponents(t, s);
            }

            @Override
            protected Reader initReader(String campo, Reader r) {
                return plegado == null ? r : new MappingCharFilter(plegado, r);
            }
        };
        SolrSynonymParser parser = new SolrSynonymParser(true, true, reglas);
        try {
            for (Path a : archivos) {
                // Se quitan los comentarios a mano: el parser de Solr solo
                // acepta # al principio de línea, igual que aquí.
                parser.parse(Files.newBufferedReader(a, StandardCharsets.UTF_8));
            }
            return parser.build();
        } catch (ParseException e) {
            throw new IOException("error leyendo sinónimos: " + e.getMessage(), e);
        }
    }

    @Override
    protected Reader initReader(String campo, Reader r) {
        if (fechas || numeros) r = new NormalizadorCharFilter(r, fechas, numeros);
        if (plegado != null) r = new MappingCharFilter(plegado, r);
        return r;
    }

    @Override
    protected TokenStreamComponents createComponents(String campo) {
        Tokenizer t = new PatternTokenizer(TOKEN, 0);
        TokenStream s = t;
        if (minusculas) s = new LowerCaseFilter(s);
        s = new LengthFilter(s, largoMin, largoMax);
        if (sinonimos != null) {
            s = new SynonymGraphFilter(s, sinonimos, false);
            s = new FlattenGraphFilter(s);
        }
        if (stop != null) s = new StopFilter(s, stop);
        if (porter) s = new PorterStemFilter(s);
        return new TokenStreamComponents(t, s);
    }
}
