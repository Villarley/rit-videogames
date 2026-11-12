package rit;

import java.io.BufferedReader;
import java.io.FileDescriptor;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStreamReader;
import java.io.PrintStream;
import java.io.StringReader;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.ResultSet;
import java.sql.Statement;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicLong;
import java.util.stream.Stream;
import org.apache.lucene.analysis.TokenStream;
import org.apache.lucene.analysis.tokenattributes.CharTermAttribute;
import org.apache.lucene.document.Document;
import org.apache.lucene.document.Field;
import org.apache.lucene.document.FieldType;
import org.apache.lucene.document.StoredField;
import org.apache.lucene.index.DirectoryReader;
import org.apache.lucene.index.IndexOptions;
import org.apache.lucene.index.IndexWriter;
import org.apache.lucene.index.IndexWriterConfig;
import org.apache.lucene.index.MultiTerms;
import org.apache.lucene.index.Terms;
import org.apache.lucene.index.TermsEnum;
import org.apache.lucene.queryparser.classic.QueryParser;
import org.apache.lucene.search.IndexSearcher;
import org.apache.lucene.search.Query;
import org.apache.lucene.search.ScoreDoc;
import org.apache.lucene.search.TopDocs;
import org.apache.lucene.search.similarities.ClassicSimilarity;
import org.apache.lucene.store.FSDirectory;
import org.apache.lucene.util.BytesRef;

/**
 * Indexador con Apache Lucene. Programa aparte de la araña: lee crawl.db y los .txt.
 *
 * <pre>
 * java -jar lucene/target/indexador-lucene.jar indexar config/completo.conf [--limite N] [--hilos N]
 * java -jar lucene/target/indexador-lucene.jar volcar  config/completo.conf > vocab.tsv
 * java -jar lucene/target/indexador-lucene.jar analizar config/completo.conf < texto.txt
 * java -jar lucene/target/indexador-lucene.jar buscar  config/completo.conf "elden ring boss"
 * </pre>
 */
public final class Principal {
    static final String CAMPO = "contenido";

    public static void main(String[] args) throws Exception {
        System.setOut(new PrintStream(new FileOutputStream(FileDescriptor.out), true, StandardCharsets.UTF_8));
        if (args.length < 2) {
            System.err.println("uso: indexar|volcar|analizar|buscar <archivo.conf> [opciones]");
            System.exit(2);
        }
        Path conf = Path.of(args[1]).toAbsolutePath();
        Path raizRepo = raizRepo();
        Configuracion c = new Configuracion(conf, raizRepo);
        switch (args[0]) {
            case "indexar" -> indexar(c, args);
            case "volcar" -> volcar(c);
            case "analizar" -> analizar(c);
            case "buscar" -> buscar(c, args.length > 2 ? args[2] : "");
            default -> {
                System.err.println("comando desconocido: " + args[0]);
                System.exit(2);
            }
        }
    }

    /** La raíz del repo es la carpeta que tiene config/ (se puede cambiar con -Drit.raiz=...). */
    static Path raizRepo() {
        String p = System.getProperty("rit.raiz");
        if (p != null) return Path.of(p).toAbsolutePath();
        Path d = Path.of("").toAbsolutePath();
        for (Path x = d; x != null; x = x.getParent()) {
            if (Files.isDirectory(x.resolve("config")) && Files.isDirectory(x.resolve("indexer"))) return x;
        }
        return d;
    }

    record Doc(int pageId, String ruta) {}

    static List<Doc> listar(Path base, int limite) throws Exception {
        List<Doc> docs = new ArrayList<>();
        String sql = "SELECT id, text_path FROM pages WHERE text_path IS NOT NULL AND text_path != '' ORDER BY id"
                + (limite > 0 ? " LIMIT " + limite : "");
        try (Connection cn = DriverManager.getConnection("jdbc:sqlite:file:" + base + "?mode=ro");
             Statement st = cn.createStatement();
             ResultSet rs = st.executeQuery(sql)) {
            while (rs.next()) docs.add(new Doc(rs.getInt(1), rs.getString(2)));
        }
        return docs;
    }

    static int opcion(String[] args, String nombre, int porDefecto) {
        for (int i = 2; i < args.length - 1; i++) if (args[i].equals(nombre)) return Integer.parseInt(args[i + 1]);
        return porDefecto;
    }

    static long tamanoCarpeta(Path p) throws IOException {
        try (Stream<Path> s = Files.walk(p)) {
            return s.filter(Files::isRegularFile).mapToLong(f -> f.toFile().length()).sum();
        }
    }

    static void indexar(Configuracion c, String[] args) throws Exception {
        long t0 = System.nanoTime();
        int limite = opcion(args, "--limite", 0);
        int hilos = opcion(args, "--hilos", c.entero("lucene", "hilos", 0));
        if (hilos <= 0) hilos = Runtime.getRuntime().availableProcessors();
        Path raiz = c.ruta("coleccion", "raiz", "repository");
        Path base = c.ruta("coleccion", "base", "repository/crawl.db");
        Path salida = c.ruta("lucene", "salida", "indices/lucene");
        String nombre = c.texto("indice", "nombre", "indice");
        boolean fusionar = c.booleano("lucene", "fusionar", true);

        AnalizadorRit analizador = new AnalizadorRit(c);
        List<Doc> docs = listar(base, limite);
        System.out.printf(Locale.ROOT, "[lucene-%s] %,d documentos, política %s, %d hilos%n",
                nombre, docs.size(), analizador.resumen, hilos);

        if (Files.exists(salida)) {
            try (Stream<Path> s = Files.walk(salida)) {
                s.sorted((a, b) -> b.compareTo(a)).forEach(x -> x.toFile().delete());
            }
        }
        Files.createDirectories(salida);

        // Mismo modelo que el índice propio: solo documentos y frecuencias
        // (sin posiciones) y similitud TF-IDF del modelo vectorial clásico.
        FieldType tipo = new FieldType();
        tipo.setIndexOptions(IndexOptions.DOCS_AND_FREQS);
        tipo.setTokenized(true);
        tipo.setStored(false);
        tipo.setOmitNorms(false);
        tipo.freeze();

        IndexWriterConfig iwc = new IndexWriterConfig(analizador);
        iwc.setSimilarity(new ClassicSimilarity());
        iwc.setOpenMode(IndexWriterConfig.OpenMode.CREATE);
        iwc.setRAMBufferSizeMB(c.entero("lucene", "ram_mb", 512));
        iwc.setUseCompoundFile(false);

        AtomicLong bytes = new AtomicLong();
        AtomicLong hechos = new AtomicLong();
        long tIndexar;
        long tFusion = 0;
        try (FSDirectory dir = FSDirectory.open(salida); IndexWriter w = new IndexWriter(dir, iwc)) {
            ExecutorService pool = new ThreadPoolExecutor(hilos, hilos, 0, TimeUnit.SECONDS,
                    new ArrayBlockingQueue<>(hilos * 64), new ThreadPoolExecutor.CallerRunsPolicy());
            long t1 = System.nanoTime();
            for (Doc d : docs) {
                pool.execute(() -> {
                    try {
                        byte[] crudo = Files.readAllBytes(raiz.resolve(d.ruta()));
                        bytes.addAndGet(crudo.length);
                        Document doc = new Document();
                        doc.add(new StoredField("page_id", d.pageId()));
                        doc.add(new Field(CAMPO, new String(crudo, StandardCharsets.UTF_8), tipo));
                        w.addDocument(doc);
                    } catch (IOException e) {
                        // archivo que falta: se indexa vacío para que los números de documento coincidan
                        try {
                            Document doc = new Document();
                            doc.add(new StoredField("page_id", d.pageId()));
                            w.addDocument(doc);
                        } catch (IOException e2) {
                            throw new RuntimeException(e2);
                        }
                    }
                    long n = hechos.incrementAndGet();
                    if (n % 50000 == 0) {
                        double seg = (System.nanoTime() - t1) / 1e9;
                        System.out.printf(Locale.ROOT, "  %,d docs, %,.0f MiB, %.1f MiB/s%n",
                                n, bytes.get() / 1048576.0, bytes.get() / 1048576.0 / seg);
                    }
                });
            }
            pool.shutdown();
            pool.awaitTermination(365, TimeUnit.DAYS);
            w.commit();
            tIndexar = System.nanoTime() - t1;
            if (fusionar) {
                long t2 = System.nanoTime();
                w.forceMerge(1);
                w.commit();
                tFusion = System.nanoTime() - t2;
            }
        }
        double total = (System.nanoTime() - t0) / 1e9;

        long vocabulario = 0;
        long postings;
        long tokens;
        try (DirectoryReader r = DirectoryReader.open(FSDirectory.open(salida))) {
            Terms terms = MultiTerms.getTerms(r, CAMPO);
            if (terms != null) {
                TermsEnum te = terms.iterator();
                while (te.next() != null) vocabulario++;
                postings = terms.getSumDocFreq();
                tokens = terms.getSumTotalTermFreq();
            } else {
                postings = tokens = 0;
            }
        }
        long tam = tamanoCarpeta(salida);
        String json = String.format(Locale.ROOT, """
                {
                  "indexador": "lucene",
                  "politica": "%s",
                  "normalizacion": "%s",
                  "archivo": "%s",
                  "documentos": %d,
                  "vocabulario": %d,
                  "tokens": %d,
                  "postings": %d,
                  "bytes_texto": %d,
                  "bytes_indice": %d,
                  "proporcion_indice_texto": %.6f,
                  "hilos": %d,
                  "fusionar": %s,
                  "segundos": {"indexar": %.3f, "fusion": %.3f, "total": %.3f},
                  "mib_por_segundo": %.3f
                }
                """, nombre, analizador.resumen, salida.toString().replace("\\", "/"), docs.size(), vocabulario,
                tokens, postings, bytes.get(), tam, bytes.get() == 0 ? 0.0 : (double) tam / bytes.get(), hilos, fusionar,
                tIndexar / 1e9, tFusion / 1e9, total, bytes.get() / 1048576.0 / total);
        Files.writeString(Path.of(salida + ".json"), json);
        System.out.printf(Locale.ROOT, "[lucene-%s] listo: %,d términos, %,d postings, %.1f MiB (%.1f%% del texto), %.1f s%n",
                nombre, vocabulario, postings, tam / 1048576.0, 100.0 * tam / Math.max(bytes.get(), 1), total);
    }

    /** término TAB df TAB cf, en orden de bytes (el mismo orden del .idx). */
    static void volcar(Configuracion c) throws IOException {
        Path salida = c.ruta("lucene", "salida", "indices/lucene");
        PrintStream out = new PrintStream(System.out, false, StandardCharsets.UTF_8);
        try (DirectoryReader r = DirectoryReader.open(FSDirectory.open(salida))) {
            Terms terms = MultiTerms.getTerms(r, CAMPO);
            if (terms == null) return;
            TermsEnum te = terms.iterator();
            BytesRef t;
            while ((t = te.next()) != null) {
                out.print(t.utf8ToString());
                out.print('\t');
                out.print(te.docFreq());
                out.print('\t');
                out.println(te.totalTermFreq());
            }
        }
        out.flush();
    }

    /** Lee texto de la entrada y escribe un término por línea (para comparar con Python). */
    static void analizar(Configuracion c) throws IOException {
        AnalizadorRit a = new AnalizadorRit(c);
        BufferedReader in = new BufferedReader(new InputStreamReader(System.in, StandardCharsets.UTF_8));
        StringBuilder sb = new StringBuilder();
        char[] buf = new char[8192];
        int n;
        while ((n = in.read(buf)) > 0) sb.append(buf, 0, n);
        PrintStream out = new PrintStream(System.out, false, StandardCharsets.UTF_8);
        try (TokenStream ts = a.tokenStream(CAMPO, new StringReader(sb.toString()))) {
            CharTermAttribute term = ts.addAttribute(CharTermAttribute.class);
            ts.reset();
            while (ts.incrementToken()) out.println(term.toString());
            ts.end();
        }
        out.flush();
    }

    static void buscar(Configuracion c, String consulta) throws Exception {
        Path salida = c.ruta("lucene", "salida", "indices/lucene");
        AnalizadorRit a = new AnalizadorRit(c);
        try (DirectoryReader r = DirectoryReader.open(FSDirectory.open(salida))) {
            IndexSearcher s = new IndexSearcher(r);
            s.setSimilarity(new ClassicSimilarity());
            Query q = new QueryParser(CAMPO, a).parse(QueryParser.escape(consulta));
            long t0 = System.nanoTime();
            TopDocs top = s.search(q, 10);
            double ms = (System.nanoTime() - t0) / 1e6;
            System.out.printf(Locale.ROOT, "%s  (%,d resultados, %.2f ms)%n", q, top.totalHits.value, ms);
            for (ScoreDoc sd : top.scoreDocs) {
                Document d = s.storedFields().document(sd.doc);
                System.out.printf(Locale.ROOT, "  %.4f  page_id=%s%n", sd.score, d.get("page_id"));
            }
        }
    }
}
